# encoding: utf-8
"""
This module deals primarily with authentication, as well as providing some
authorization helpers that inspect authentication state.

"""
import calendar
import logging
import OpenSSL.crypto as ssl
import os.path

from datetime import datetime, timedelta
from functools import partial

from pyramid.interfaces import IAuthenticationPolicy
from pyramid.security import Authenticated, Everyone
from pyramid.settings import asbool
from sqlalchemy.orm import joinedload_all
from sqlalchemy.orm.exc import NoResultFound
from zope.interface import implements

from floof import model
from floof.lib.authz import TRUST_MAP
from floof.lib.authz import add_user_authz_methods

log = logging.getLogger(__name__)

DEFAULT_CONFIDENCE_EXPIRY = 60 * 10  # seconds


class FloofAuthnPolicy(object):
    """Pyramid style authentication policy bolted atop a beaker session.

    Most of the actual work here is done by :class:`Authenticizer`, an instance
    of which should be attached to the `request` as ``request.auth``.

    The Pyramid auth interface is extremely clunky, and this class only exists
    so standard Pyramid authorization stuff still works.

    """
    implements(IAuthenticationPolicy)

    def authenticated_userid(self, request):
        raise NotImplementedError()

    def unauthenticated_userid(self, request):
        raise NotImplementedError()

    def effective_principals(self, request):
        """Returns the list of 'effective' :term:`principal` identifiers for
        the request."""

        user = request.auth.user
        principals = set([Everyone])

        if not user:
            return principals

        principals.add(Authenticated)
        principals.update(['user:' + str(user.id)])
        principals.update('role:' + role.name for role in user.roles)
        principals.update('trusted:' + flag for flag in request.auth.trust)

        if user.cert_auth in ('required', 'sensitive_required'):
            principals.add('auth:secure')
        else:
            principals.add('auth:insecure')

        # Add derived principals
        for derivative, prereqs_list in TRUST_MAP.iteritems():
            for prereqs in prereqs_list:
                f = lambda x: x in principals
                if all(map(f, prereqs)):
                    principals.add(derivative)
                    break

        return principals

    def remember(self, request, user, openid_url=None, persona_addr=None, **kw):
        """Remembers a set of (stateful) credentials authenticating the `user`.

        Deviates from the Pyramid authentication policy model in that
        `principal` is the user (as an SQLAlchemy model object) not a
        principal.

        At present, only accepts calls that include both a `user` and either a
        `openid_url` or `persona_addr` parameter.

        Raises Exceptions on error; either `ValueError` if no parameters are
        given or one of the auth-specific exceptions defined in
        :mod:`floof.lib.auth`.

        """
        if openid_url:
            request.auth.login_openid(request, user, openid_url)
        elif persona_addr:
            request.auth.login_persona(request, user, persona_addr)
        else:
            raise ValueError("A credential, such as openid_url, must be "
                             "passed to this function.")

        # Renew session identifier on persistant (non-cert) authn status change
        # (a best practice).  Such renewal is not part of the Pyramid session
        # interface, so conditionally check for the regenerate_id method that
        # is present on Beaker sessions
        if hasattr(request.session, 'regenerate_id'):
            request.session.regenerate_id()

        request.session.save()
        return []

    def forget(self, request):
        """Purges all purgable authentication data from the
        :class:`Authenticizer` at ``request.auth``."""

        request.auth.clear()
        request.session.save()
        return []


class CertNotFoundError(Exception): pass
class CertAuthDisabledError(Exception): pass
class CertExpiredError(Exception): pass
class CertRevokedError(Exception): pass
class CertVerificationError(Exception): pass
class OpenIDAuthDisabledError(Exception): pass
class OpenIDNotFoundError(Exception): pass
class PersonaAuthDisabledError(Exception): pass
class PersonaNotFoundError(Exception): pass
class AuthConflictError(Exception): pass


class Authenticizer(object):
    """Manages the authentication and authorization state of the current user.

    This class is intended to be instantiated from a Request object.  The
    :class:`FloofAuthnPolicy` class expects to find an instance of this class
    on the request, and delegates Pyramid security functionality here.

    To perform authentication and identity resolution, the constructor calls a
    series of methods, one for each supported authentication mechanism.  These
    methods are prefixed with ``check_`` and have the following obligations:

    1. If they cannot resolve a valid authentication, they must clear any
       authentication information related to the authentication mechanism that
       they handle from :attr:`state`;

    2. If they resolve a valid authentication, and this differs from the
       prevailing value of :attr:`user`, they again must clear any
       authentication information that they may have set (in a previous
       request) from :attr:`state`;

    3. If they resolve a valid authentication, and this agrees with the
       prevailing value of :attr:`user` (or :attr:`user` is None), they must
       add any related authentication information to :attr:`state` and append
       appropriate flags to :attr:`trust`.  If :attr:`user` is None, they must
       set it to the resolved user; and

    4. If the authentication fails or is invalid or inconsistent, they should
       raise a relevant error and be sure to catch it in the constructor.

    Note: everything within :attr:`state` is meant to be consistent at all
    times; i.e., there should never be a cert serial, openid, or other
    credential token that don't all resolve to the same user, and the
    constructor should ensure this.

    """
    # These are used for injecting tokens and trust flags during tests
    _cred_tokens = ['cert_serial', 'openid_url', 'openid_timestamp',
                    'persona_addr', 'persona_timestamp']
    _trust_flags = ['cert', 'openid', 'openid_recent', 'persona',
                    'persona_recent']

    def __init__(self, request):
        config = request.registry.settings

        # Attributes
        self.state = request.session.setdefault('auth', {})
        """A reference to `request.session['auth']`, a dictionary that contains
        all of the state information needed by :class:`Authenticizer` between
        requests."""

        self.user = model.AnonymousUser()
        """Either the user authenticated by the state of the various
        authentication mechanisms of :class:`Authenticizer` (listed in
        :attr:`trust`) or an instance of :class:`floof.model.AnonymousUser`."""

        self.trust = []
        """A list of authentication mechanisms satisfied that the current
        request authenticates :attr:`user`."""

        # convenience/readability helper
        error = partial(request.session.flash, level='error',
                        icon='key--exclamation', allow_duplicate=False)

        if 'paste.testing' in request.environ:
            self._setup_testing_early(request)

        try:
            self.check_certificate(request)
        except CertNotFoundError:
            log.error("A validated client cert was not found in the DB, yet "
                      "client certs should persist in the DB indefinately.")
            error("I don't recognize your client certificate.")
        except CertVerificationError:
            error("The client certificate you are presenting is invalid.")
        except CertAuthDisabledError:
            error("You are presenting a valid certificate, but client certificates are disabled for your account.")
        except CertExpiredError:
            error("The client certificate you are presenting has expired.")
        except CertRevokedError:
            error("The client certificate you are presenting has been revoked.")

        try:
            self.check_openid(config)
        except OpenIDNotFoundError:
            error("I don't recognize your OpenID identity.")
        except OpenIDAuthDisabledError:
            error("Your OpenID is no longer accepted as your account has disabled OpenID authentication.")
        except AuthConflictError:
            error("Your OpenID conflicted with your certificate and has been cleared.")

        try:
            self.check_persona(config)
        except PersonaNotFoundError:
            error("I don't recognize your Persona email address.")
        except PersonaAuthDisabledError:
            error("Your Persona is no longer accepted as your account has disabled Persona authentication.")
        except AuthConflictError:
            error("Your Persona conflicted with either your certificate or "
                  "your OpenID and has been cleared.")

        if 'paste.testing' in request.environ:
            self._setup_testing_late(request)

        if len(self.trust) == 0:
            # Either there's no user, or none of their current auths are valid.
            # Wipe the slate clean
            self.clear()

        print self, request.method, request.url
        request.session.changed()

        # This invocation is for the benefit of currently-logged-in
        # sensitive_required users only
        check_certreq_override(request, self.user)

        # Add .can and .permitted
        add_user_authz_methods(self.user, request)

    def _setup_testing_early(self, request):
        """Setup any requested test credential tokens."""

        self.clear()
        env = request.environ

        if 'tests.user_id' in env:
            self.user = model.session.query(model.User).get(
                    env['tests.user_id'])

        else:
            for token in self._cred_tokens:
                idx = 'tests.auth.' + token
                if idx in env:
                    self.state[token] = env[idx]

    def _setup_testing_late(self, request):
        """Override trust flags as requested or required."""

        env = request.environ
        if ('tests.auth_trust' in env or 'tests.user_id' in env):
            self.trust = env.get('tests.auth_trust', self._trust_flags)

    def check_certificate(self, request):
        """Check a client certificate serial and add authentication if valid."""

        self.state.pop('cert_serial', None)
        serial = get_certificate_serial(request)

        if not serial:
            # No cert. Our obligation to wipe cert state is fulfilled above.
            return

        # Figure out what certificate and user this serial belongs to
        # TODO: Optimize eagerloading
        serial = serial.lower()
        q = model.session.query(model.Certificate) \
            .options(joinedload_all('user.roles')) \
            .filter_by(serial=serial)

        try:
            cert = q.one()
        except NoResultFound:
            raise CertNotFoundError

        if cert.user.cert_auth == u'disabled':
            raise CertAuthDisabledError
        if cert.expired:
            raise CertExpiredError
        if cert.revoked:
            raise CertRevokedError
        if self.user and self.user.id != cert.user_id:
            raise AuthConflictError

        # At this point, we're confident that the supplied cert is valid

        self.state['cert_serial'] = serial
        self.trust.append('cert')

        if not self.user:
            self.user = cert.user

    def check_openid(self, config):
        """Check OpenID state and add authentication if valid, else clear."""

        url = self.state.pop('openid_url', None)
        timestamp = self.state.pop('openid_timestamp', None)

        if not url or timestamp is None:
            # No (or corrupted) OpenID login. By popping, our obligation to
            # clear relevent state is already fulfilled, so just return
            return

        # TODO: Optimize eagerloading
        q = model.session.query(model.IdentityURL) \
            .options(joinedload_all('user.roles')) \
            .filter_by(url=url)

        try:
            openid = q.one()
        except NoResultFound:
            raise OpenIDNotFoundError

        if openid.user.cert_auth == 'required':
            raise OpenIDAuthDisabledError
        if self.user and self.user.id != openid.user_id:
            raise AuthConflictError

        # XXX Check timestamp sanity?
        # At this point, we're confident that the stored OpenID login is valid

        self.state['openid_url'] = url
        self.state['openid_timestamp'] = timestamp
        self.trust.append('openid')

        # Evaluate OpenID freshness
        confidence_expiry_secs = int(config.get(
            'auth.openid.expiry_seconds',
            DEFAULT_CONFIDENCE_EXPIRY))

        age = datetime.utcnow() - datetime.utcfromtimestamp(timestamp)
        if age <= timedelta(seconds=confidence_expiry_secs):
            self.trust.append('openid_recent')

        if not self.user:
            self.user = openid.user

    def check_persona(self, config):
        """Check Persona state and add authentication if valid, else
        clear."""
        # XXX this is very similar to check_openid() above

        addr = self.state.pop('persona_addr', None)
        timestamp = self.state.pop('persona_timestamp', None)

        if not addr or not timestamp:
            # No (or corrupted) Persona login. By popping, our obligation to
            # clear relevent state is already fulfilled, so just return
            return

        # TODO: Optimize eagerloading
        q = model.session.query(model.IdentityEmail) \
            .options(joinedload_all('user.roles')) \
            .filter_by(email=addr)

        try:
            persona = q.one()
        except NoResultFound:
            raise PersonaNotFoundError

        if persona.user.cert_auth == 'required':
            raise PersonaAuthDisabledError
        if self.user and self.user.id != persona.user.id:
            raise AuthConflictError

        # At this point, we're confident that the stored Persona login is valid

        self.state['persona_addr'] = addr
        self.state['persona_timestamp'] = timestamp
        self.trust.append('persona')

        # Evaluate Persona freshness
        confidence_expiry_secs = int(config.get(
            'auth.persona.expiry_seconds',
            DEFAULT_CONFIDENCE_EXPIRY))

        age = datetime.utcnow() - datetime.utcfromtimestamp(timestamp)
        if age <= timedelta(seconds=confidence_expiry_secs):
            self.trust.append('persona_recent')

        if not self.user:
            self.user = persona.user

    def login_openid(self, request, user, url):
        """Log in via OpenID, adding appropriate authentication state.

        Remember that any authentication change will only take effect on the
        next request.  The typical scenario is that the user is redirected at
        the end of a request that calls this method.

        Also remember to save the session after this!
        """
        if not url in (u.url for u in user.identity_urls):
            raise OpenIDNotFoundError

        check_certreq_override(request, user)

        if user.cert_auth == 'required':
            raise OpenIDAuthDisabledError

        self.state['openid_url'] = url
        self.state['openid_timestamp'] = calendar.timegm(datetime.utcnow().timetuple())

    def login_persona(self, request, user, addr):
        """Log in via Persona, adding appropriate authentication state.

        Remember that any authentication change will only take effect on the
        next request.  The typical scenario is that the user is redirected at
        the end of a request that calls this method.

        Also remember to save the session after this!
        """
        if addr not in (e.email for e in user.identity_emails):
            raise PersonaNotFoundError

        check_certreq_override(request, user)

        if user.cert_auth == 'required':
            raise PersonaAuthDisabledError

        self.state['persona_addr'] = addr
        self.state['persona_timestamp'] = calendar.timegm(datetime.utcnow().timetuple())

    def clear(self):
        """Clears all auth state, logging out unless certs are in use."""
        self.state.clear()
        self.user = model.AnonymousUser()
        self.trust = []

    # Provide implementation-independent introspection of credential tokens
    def _get_state(key):
        return lambda self: self.state.get(key)

    certificate_serial = property(_get_state('cert_serial'))
    openid_url = property(_get_state('openid_url'))
    persona_addr = property(_get_state('persona_addr'))

    def __repr__(self):
        ages = {}
        for mech in ('openid', 'persona'):
            idx = mech + '_timestamp'
            if idx in self.state:
                age = datetime.utcnow() - datetime.utcfromtimestamp(self.state[idx])
                ages[mech] = age

        get = self.state.get

        ret = '<Authenticizer ( User: {0}, '.format(
                self.user.name if self.user else None)

        if get('cert_serial'):
            ret += 'Cert: {0}, '.format(get('cert_serial'))
        if get('openid_url'):
            ret += 'OpenID: {0} @ {1}, '.format(
                    get('openid_url'), ages.get('openid'))
        if get('persona_addr'):
            ret += 'Persona: {0} @ {1}, '.format(
                    get('persona_addr'), ages.get('persona'))
        ret += 'Trust Flags: {0} )>'.format(repr(self.trust))

        return ret

def get_certificate_serial(request):
    """Return a verified certificate serial from `request`, if any, else None.

    Raises CertVerificationError if a certificate has been sent but was invalid
    according to the front-end server that handled the SSL connection.

    Currently assumes the use of nginx as the front-end server and SSL
    endpoint.

    """
    # test amenity
    env = request.environ
    settings = request.registry.settings
    if 'paste.testing' in env and 'tests.auth.cert_serial' in env:
        return env['tests.auth.cert_serial']

    if not asbool(settings.get('auth.certs.enabled')):
        return

    # ATM, the cert serial is passed by the front-end server in an HTTP header.
    transport = settings.get('auth.certs.transport')
    if transport == 'http_headers':
        verify = request.headers.get('X-Floof-SSL-Client-Verify')
        serial = request.headers.get('X-Floof-SSL-Client-Serial', '')
    elif transport == 'wsgi_environ':
        verify = request.environ.get('floof.ssl_client_verify')
        serial = request.environ.get('floof.ssl_client_serial', '')
    else:
        log.warning('Certificate auth enabled, but no valid transport set.')
        return

    serial = serial.lower()

    # Need to check verification status as we anticipate still passing
    # requests that fail the front-end cert verification back to floof
    # XXX These 'verify' codes are nginx-isms
    if verify == 'SUCCESS':
        log.debug("Successful verification of cert with claimed "
                  "serial '{0}'".format(serial))
        return serial

    elif verify == 'FAILED':
        log.warning("Unsuccessful verification of cert with claimed "
                    "serial '{0}'".format(serial))
        raise CertVerificationError

    elif verify != 'NONE':
        log.warning('Unknown cert verification status')


def check_certreq_override(request, user):
    """To prevent fequent accidental lockout, set cert_auth option to
    "allowed" if the user has no valid certs."""
    if (user and
            not user.valid_certificates and
            user.cert_auth in ('required', 'sensitive_required')):
        user.cert_auth = 'allowed'
        request.session.flash(
                "You no longer have any valid certificates, so your "
                "<a href=\"{0}\">Authentication Option</a> has been reset "
                "to 'Allowed for login'"
                .format(request.route_url('controls.auth')),
                level='warning', html_escape=False)


def get_ca(settings):
    """Fetches the Certifiacte Authority certificate and key.

    Returns a (ca_cert, ca_key) tuple, where ca_cert is a pyOpenSSL
    X509 object and ca_key is a pyOpenSSL PKey object.

    `settings` is a Pyramid `deployment settings` object, typically
    ``request.registry.settings``

    """
    cert_dir = settings['auth.certs.directory']
    ca_cert_file = os.path.join(cert_dir, 'ca.pem')
    ca_key_file = os.path.join(cert_dir, 'ca.key')

    with open(ca_cert_file, 'rU') as f:
        ca_cert = ssl.load_certificate(ssl.FILETYPE_PEM, f.read())

    with open(ca_key_file, 'rU') as f:
        ca_key = ssl.load_privatekey(ssl.FILETYPE_PEM, f.read())

    return ca_cert, ca_key
