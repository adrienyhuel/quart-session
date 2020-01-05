# -*- coding: utf-8 -*-
"""
    quart_session.sessions
    ~~~~~~~~~~~~~~~~~~~~~~

    Server-side Sessions and SessionInterfaces.

    :copyright: (c) 2014 by Shipeng Feng.
    :copyright: (c) 2020 by dsc.
    :license: BSD, see LICENSE for more details.
"""
import time
from typing import Any, Callable, Optional, TYPE_CHECKING
from uuid import uuid4
import asyncio

from quart import Quart
from quart.wrappers import BaseRequestWebsocket, Response
from quart.wrappers.response import FileBody
from quart.sessions import SessionInterface as QuartSessionInterface, SecureCookieSession
from quart.json.tag import TaggedJSONSerializer
from itsdangerous import Signer, BadSignature, want_bytes


def total_seconds(td):
    return td.days * 60 * 60 * 24 + td.seconds


class ServerSideSession(SecureCookieSession):
    """Baseclass for server-side based sessions."""

    def __init__(self, initial=None, sid=None, permanent=None, addr=None):
        super(ServerSideSession, self).__init__(**initial or {})
        self.sid = sid
        if permanent:
            self.permanent = permanent
        if addr:
            self.addr = addr
        self._dirty = False

    @property
    def addr(self) -> str:
        return self.get('_addr', False)  # type: ignore

    @addr.setter
    def addr(self, value: str) -> None:
        self['_addr'] = value  # type: ignore

    def dirty(self):
        """Marks the session to be written/saved.

        .. note::

            This feature only works if you have set ``SESSION_EXPLICIT``
            to ``True``, at which point you'll have to explicitly mark
            each session before they'll get processed and saved.

        Example::

            app.config['SESSION_EXPLICIT'] = True
            Session(app)

            @app.route('/')
            def root():
                session['foo'] = 'bar'
                session.dirty()
                return "Hello World!"
        """
        self._dirty = True


class RedisSession(ServerSideSession):
    pass


class MemcachedSession(ServerSideSession):
    pass


class NullSession(ServerSideSession):
    pass


class SessionInterface(QuartSessionInterface):
    """Baseclass for session interfaces"""

    serializer = TaggedJSONSerializer()
    session_class = None

    def __init__(
            self,
            key_prefix: str,
            use_signer: bool = False,
            permanent: bool = True,
            **kwargs
    ) -> None:
        self.key_prefix = key_prefix
        self.use_signer = use_signer
        self.permanent = permanent
        self._config = kwargs

    async def open_session(
            self,
            app: Quart,
            request: BaseRequestWebsocket
    ) -> Optional[SecureCookieSession]:
        sid = request.cookies.get(app.session_cookie_name)
        addr = request.headers.get('X-Forwarded-For', request.remote_addr) if \
            self._config['SESSION_HIJACK_PROTECTION'] else None
        options = {"sid": sid, "permanent": self.permanent, "addr": addr}

        if not sid:
            options['sid'] = self._generate_sid()
            return self.session_class(**options)
        if self.use_signer:
            signer = self._get_signer(app)
            if signer is None:
                app.logger.warning("Failed to obtain a valid signer.")
                return None
            try:
                sid_as_bytes = signer.unsign(sid)
                sid = sid_as_bytes.decode()
            except BadSignature:
                app.logger.warning(f"Bad signature for sid: {sid}.")
                options['sid'] = self._generate_sid()
                return self.session_class(**options)

        val = await self._backend_get(app, self.key_prefix + sid)
        if val is None:
            options['sid'] = self._generate_sid()
            return self.session_class(**options)

        try:
            data = self.serializer.loads(val)
        except:
            app.logger.warning(f"Failed to deserialize session "
                               f"data for sid: {sid}. Generating new sid.")
            options['sid'] = self._generate_sid()
            return self.session_class(**options)

        prevent_hijack = self._config['SESSION_HIJACK_PROTECTION']
        if prevent_hijack is True:
            if self._config['SESSION_HIJACK_REVERSE_PROXY'] is True:
                addr = request.headers.get('X-Forwarded-For', request.remote_addr)
            else:
                addr = request.remote_addr
            if data.get('_addr', addr) != addr:
                await self._backend_delete(app, self.key_prefix + sid)
                options['sid'] = self._generate_sid()
                return self.session_class(**options)

        res = self.session_class(data, sid)
        return res

    async def save_session(  # type: ignore
        self,
        app: "Quart",
        session: SecureCookieSession,
        response: Response
    ) -> None:
        # prevent set-cookie
        if self._config['SESSION_EXPLICIT'] is True and \
                not session._dirty:
            return

        # prevent set-cookie on (static) file responses
        # https://github.com/fengsp/flask-session/pull/70
        if self._config['SESSION_STATIC_FILE'] is False and \
                isinstance(response.response, FileBody):
            return
        session_key = self.key_prefix + session.sid
        domain = self.get_cookie_domain(app)
        path = self.get_cookie_path(app)
        if not session:
            if session.modified:
                await self._backend_delete(app=app, key=session_key)
                response.delete_cookie(app.session_cookie_name,
                                       domain=domain, path=path)
            return
        httponly = self.get_cookie_httponly(app)
        secure = self.get_cookie_secure(app)
        expires = self.get_expiration_time(app, session)

        val = self.serializer.dumps(dict(session))
        await self._backend_set(app=app, key=session_key, value=val)
        if self.use_signer:
            session_id = self._get_signer(app).sign(want_bytes(session.sid))
        else:
            session_id = session.sid
        response.set_cookie(app.session_cookie_name, session_id,
                            expires=expires, httponly=httponly,
                            domain=domain, path=path, secure=secure)

    async def create(self, app: Quart):
        raise NotImplementedError()

    async def _backend_get(self, app: Quart, key: str):
        raise NotImplementedError()

    async def _backend_set(self, app: Quart, key: str, value):
        raise NotImplementedError()

    async def _backend_delete(self, app: Quart, key: str):
        raise NotImplementedError()

    def _generate_sid(self) -> str:
        return str(uuid4())

    def _get_signer(self, app) -> Optional[Signer]:
        if not app.secret_key:
            return None
        return Signer(app.secret_key, salt='quart-session',
                      key_derivation='hmac')


class RedisSessionInterface(SessionInterface):
    """Uses the Redis key-value store as a session backend.

    :param redis: ``aioredis.Redis`` instance.
    :param key_prefix: A prefix that is added to all Redis store keys.
    :param use_signer: Whether to sign the session id cookie or not.
    :param permanent: Whether to use permanent session or not.
    :param kwargs: Quart-session config, used internally.
    """

    session_class = RedisSession

    def __init__(self, redis, **kwargs):
        super(RedisSessionInterface, self).__init__(**kwargs)
        self.redis = redis

    async def create(self, app: Quart) -> None:
        """Creates ``aioredis.Redis`` instance.

        .. note::

            Creates a single Redis connection, you might prefer
            pooling instead (see ``aioredis.Redis.create_redis_pool``)
        """
        if self.redis is None:
            import aioredis
            self.redis = await aioredis.create_redis("redis://localhost")

    async def _backend_get(self, app: Quart, key: str):
        return await self.redis.get(key)

    async def _backend_set(self, app: Quart, key: str, value):
        return await self.redis.setex(
            key=key, value=value,
            seconds=total_seconds(app.permanent_session_lifetime))

    async def _backend_delete(self, app: Quart, key: str):
        return await self.redis.delete(key)


class RedisTrioSessionInterface(SessionInterface):
    """Uses the Redis+Trio key-value store as a session backend.

    :param redis: ``quart_session.redis_trio.RedisTrio`` instance.
    :param key_prefix: A prefix that is added to all Redis store keys.
    :param use_signer: Whether to sign the session id cookie or not.
    :param permanent: Whether to use permanent session or not.
    :param kwargs: Quart-session config, used internally.
    """

    session_class = RedisSession

    def __init__(self, redis, **kwargs):
        super(RedisTrioSessionInterface, self).__init__(**kwargs)
        self.redis_trio = redis

    async def create(self, app: Quart) -> None:
        """Creates ``aioredis.Redis`` instance.

        .. note::

            Creates a single Redis connection. Pooling not
            supported yet for ``RedisTrio``.
        """
        if self.redis_trio is None:
            from quart_session.redis_trio import RedisTrio
            self.redis_trio = RedisTrio()
            await self.redis_trio.connect()

    async def _backend_get(self, app: Quart, key: str):
        data = await self.redis_trio.get(key)
        if data:
            return data.decode()

    async def _backend_set(self, app: Quart, key: str, value):
        return await self.redis_trio.setex(
            key=key, value=value,
            seconds=total_seconds(app.permanent_session_lifetime))

    async def _backend_delete(self, app: Quart, key: str):
        return await self.redis_trio.delete(key)


class MemcachedSessionInterface(SessionInterface):
    """Uses the Memcached key-value store as a session backend.

    :param client: ``aiomcache.Client`` instance.
    :param key_prefix: A prefix that is added to all Redis store keys.
    :param use_signer: Whether to sign the session id cookie or not.
    :param permanent: Whether to use permanent session or not.
    :param kwargs: Quart-session config, used internally.
    """

    session_class = MemcachedSession

    def __init__(
            self, memcached, key_prefix: str, use_signer: bool = False,
            permanent: bool = True, **kwargs):
        super(MemcachedSessionInterface, self).__init__(
            key_prefix=key_prefix, use_signer=use_signer,
            permanent=permanent, **kwargs)
        self.memcached = memcached

    @asyncio.coroutine
    def create(self, app: Quart) -> None:
        if self.memcached is None:
            import aiomcache
            loop = asyncio.get_running_loop()
            self.memcached = aiomcache.Client("127.0.0.1", 11211, loop=loop)

    def _get_memcache_timeout(self, timeout):
        """
        Memcached deals with long (> 30 days) timeouts in a special
        way. Call this function to obtain a safe value for your timeout.
        """
        if timeout > 2592000:  # 60*60*24*30, 30 days
            # See http://code.google.com/p/memcached/wiki/FAQ
            # "You can set expire times up to 30 days in the future. After that
            # memcached interprets it as a date, and will expire the item after
            # said date. This is a simple (but obscure) mechanic."
            #
            # This means that we have to switch to absolute timestamps.
            timeout += int(time.time())
        return timeout

    async def _backend_get(self, app: Quart, key: str):
        key = key.encode()
        return await self.memcached.get(key)

    async def _backend_set(self, app: Quart, key: str, value):
        key = key.encode()
        value = value.encode()
        expiry = self._get_memcache_timeout(total_seconds(
            app.permanent_session_lifetime))
        return await self.memcached.set(key=key, value=value,
                                        exptime=expiry)

    async def _backend_delete(self, app: Quart, key: str):
        key = key.encode()
        return await self.memcached.delete(key)


class NullSessionInterface(SessionInterface):
    """Used to open a :class:`quart.sessions.NullSession` instance.
    """

    def open_session(self, app: Quart, request: BaseRequestWebsocket):
        return None