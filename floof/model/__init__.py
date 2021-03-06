"""The application's model objects"""
import datetime
import hashlib
import OpenSSL.crypto as ssl
import pytz
import random
import re
import string

from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import backref, class_mapper, relation, validates
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.orm.properties import ColumnProperty
from sqlalchemy.orm.session import object_session
from sqlalchemy.schema import CheckConstraint
from sqlalchemy.types import *
from floof.model.extensions import *
from floof.model.types import *

# Thread-scoped session manager and table base class (which contains the
# metadata).  These are updated by initialize() below
session = scoped_session(sessionmaker())
TableBase = declarative_base()

def initialize(engine, extension=None):
    """Call me before using any of the tables or classes in the model"""
    # XXX: Is init_model actually used by anything?  Could these be combined?
    session.configure(bind=engine, extension=extension)
    TableBase.metadata.bind = engine
    #TableBase.metadata.create_all()


def now():
    return datetime.datetime.now(pytz.utc)


### CORE

class Resource(TableBase):
    """Art and users and perhaps other things have-a discussion, which is fine
    and dandy, but it means the discussion can't easily find its way back to
    the "discussee": there are multiple backrefs to check.

    The solution is this semi-hacky middle table that remembers the discussee's
    table name, and a dose of SQLA magic to make it all invisible.
    Art/User/etc. can still get directly to the discussion and doesn't need
    this table, but going backwards is much easier.

    The table is named "Resource" with the intention that it may later perform
    other duties, such as allowing joins from tags to anything, or facilitating
    global full-text search, or whatever.

    Kudos to zzzeek for the idea and example implementation:
    http://techspot.zzzeek.org/2007/05/29/polymorphic-associations-with-sqlalchemy/
    """
    __tablename__ = 'resources'
    id = Column(Integer, primary_key=True, nullable=False)
    type = Column(Enum(u'artwork', u'users', name='resources_type'), nullable=False)

    @property
    def member(self):
        return getattr(self, '_backref_%s' % self.type)

def make_resource_type(cls):
    """For table-classes that are resources.  Installs a backref on Resource
    that finds the original class.

    Also adds a 'discussion' association-proxy shortcut.
    """

    mapper = class_mapper(cls)
    table = mapper.local_table
    mapper.add_property('resource', relation(
        Resource,
        innerjoin=True,
        backref=backref(
            '_backref_%s' % table.name, uselist=False, innerjoin=True),
    ))

    # Attach a 'discussion' shortcut
    for resource_property in ('discussion',):
        setattr(cls, resource_property,
            association_proxy('resource', resource_property))

    return cls


### USERS

class AnonymousUser(object):
    """Fake not-logged-in user.

    Tests as false and generally responds correctly to User methods.
    """

    watches = ()

    def __nonzero__(self):
        return False
    def __bool__(self):
        return False

    def localtime(self, dt):
        """Anonymous users can suffer UTC."""
        return dt

    def can(self, permission, log=False):
        """Anonymous users aren't allowed to do anything that needs explicit
        permission.
        """
        return False

    def logged_privs(self):
        return []

class User(TableBase):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True, nullable=False)
    resource_id = Column(Integer, ForeignKey('resources.id'), nullable=False)
    name = Column(Unicode(24), nullable=False, index=True, unique=True)
    email = Column(Unicode(255))
    display_name = Column(Unicode(24), nullable=True)
    has_trivial_display_name = Column(Boolean, nullable=False, default=False)
    timezone = Column(Timezone, nullable=True)
    cert_auth = Column(Enum(
        u'disabled',
        u'allowed',
        u'sensitive_required',
        u'required',
        name='user_cert_auth'), nullable=False, default=u'disabled')

    def localtime(self, dt):
        """Return a datetime localized to this user's preferred timezone."""
        if self.timezone is None:
            return dt
        return dt.astimezone(self.timezone)

    @property
    def invalid_certificates(self):
        return [cert for cert in self.certificates if not cert.valid]

    @property
    def valid_certificates(self):
        return [cert for cert in self.certificates if cert.valid]

    @property
    def profile(self):
        """Returns the user's profile, if they have one.

        This would use ext.association_proxy, but that doesn't play nicely if
        the object to proxy is None.
        """
        if self._profile is None:
            return None
        return self._profile.content

    @profile.setter
    def profile(self, value):
        if self._profile is None:
            self._profile = UserProfile()
        self._profile.content = value


class IdentityURL(TableBase):
    __tablename__ = 'identity_urls'
    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    url = Column(Unicode(250), nullable=False, index=True, unique=True)

# My sincere apologies if anyone is miffed by my calling email addresses
# "emails"; I chose brevity over correctness. --epii
class IdentityEmail(TableBase):
    __tablename__ = 'identity_emails'
    id = Column(Integer, primary_key=True, nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    email = Column(Unicode(256), nullable=False, index=True, unique=True)

class UserWatch(TableBase):
    __tablename__ = 'user_watches'
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, primary_key=True)
    other_user_id = Column(Integer, ForeignKey('users.id'), nullable=False, primary_key=True)
    watch_upload = Column(Boolean, nullable=False, index=True, default=False)
    watch_by = Column(Boolean, nullable=False, index=True, default=False)
    watch_for = Column(Boolean, nullable=False, index=True, default=False)
    watch_of = Column(Boolean, nullable=False, index=True, default=False)
    created_time = Column(TZDateTime, nullable=False, index=True, default=now)


### ART

# TODO exif and png metadata -- do other formats have similar?  audio, video..  text?
class Artwork(TableBase):
    __tablename__ = 'artwork'
    id = Column(Integer, primary_key=True, nullable=False)
    resource_id = Column(Integer, ForeignKey('resources.id'), nullable=False)
    media_type = Column(Enum(u'image', u'text', u'audio', u'video', name='artwork_media_type'), nullable=False)
    title = Column(Unicode(133), nullable=False)
    hash = Column(Unicode(256), nullable=False, unique=True, index=True)
    uploader_user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    uploaded_time = Column(TZDateTime, nullable=False, index=True, default=now)
    created_time = Column(TZDateTime, nullable=False, index=True, default=now)
    original_filename = Column(Unicode(255), nullable=False)
    mime_type = Column(Unicode(255), nullable=False)
    file_size = Column(Integer, nullable=False)
    rating_count = Column(Integer, nullable=False, default=0)
    rating_sum = Column(Float, nullable=False, default=0)
    rating_score = Column(Float, nullable=True, default=None)
    # TODO should this (and the comment prose) be a special column type?
    remark = Column(UnicodeText, nullable=False, default=u'')

    __mapper_args__ = {'polymorphic_on': media_type}

    @property
    def resource_title(self):
        return self.title or 'Untitled'

    @property
    def filename(self):
        """Returns a suitable filename for the associated file."""
        # Current format looks like: artist1.artist2.artist3.title.id.ext
        filename_parts = []

        # User names have a minimal set of characters, so they should be safe
        # to put directly in filenames
        # TODO: when there's a concept of primary artist, use that first
        for user_artwork in self.user_artwork:
            filename_parts.append(user_artwork.user.name)
        if not filename_parts:
            # Should always have at least one username
            filename_parts.append(u'unknown')

        filename_parts.append(
            # Convert everything not a nice character to dashes
            re.sub(u'[^A-Za-z0-9]+', u'-', self.title).strip(u'-')
            or u'untitled')
        filename_parts.append(unicode(self.id))

        if self.mime_type == u'image/png':
            filename_parts.append(u'png')
        elif self.mime_type == u'image/jpeg':
            filename_parts.append(u'jpg')
        elif self.mime_type == u'image/gif':
            filename_parts.append(u'gif')

        return u'.'.join(filename_parts)


# Dynamic subclasses of the 'artwork' table for storing metadata for different
# types of media
class MediaImage(Artwork):
    __tablename__ = 'media_images'
    __mapper_args__ = {'polymorphic_identity': u'image'}
    id = Column(Integer, ForeignKey('artwork.id'), primary_key=True, nullable=False)
    height = Column(Integer, nullable=False)
    width = Column(Integer, nullable=False)
    number_of_colors = Column(Integer, nullable=False)
    # animated only
    frames = Column(Integer, nullable=True)
    length = Column(Time, nullable=True)
    # jpeg only
    quality = Column(Integer, nullable=True)

class MediaText(Artwork):
    __tablename__ = 'media_text'
    __mapper_args__ = {'polymorphic_identity': u'text'}
    id = Column(Integer, ForeignKey('artwork.id'), primary_key=True, nullable=False)
    words = Column(Integer, nullable=False)
    paragraphs = Column(Integer, nullable=False)


class UserArtwork(TableBase):
    __tablename__ = 'user_artwork'
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True, nullable=False, index=True)
    artwork_id = Column(Integer, ForeignKey('artwork.id'), primary_key=True, nullable=False, index=True)

class ArtworkRating(TableBase):
    """The rating that a single user has given a single piece of art"""
    __tablename__ = 'artwork_ratings'

    artwork_id = Column(Integer, ForeignKey(Artwork.id), primary_key=True, nullable=False)
    user_id = Column(Integer, ForeignKey(User.id), primary_key=True, nullable=False)
    rating = ColumnProperty(
        Column(Float, CheckConstraint('rating >= -1.0 AND rating <= 1.0'), nullable=False),
        extension=RatingAttributeExtension(),
    )
    timestamp = Column(TZDateTime, nullable=False, index=True, default=now, onupdate=now)

    validates('rating')
    def validate_rating(self, key, rating):
        """Ensures the rating is within the proper rating radius."""
        return -1.0 <= rating <= 1.0


### PERMISSIONS

class Role(TableBase):
    __tablename__ = 'roles'
    id = Column(Integer, primary_key=True, nullable=False)
    name = Column(Unicode(127), nullable=False)
    description = Column(Unicode, nullable=True)

class UserRole(TableBase):
    __tablename__ = 'user_roles'
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True, nullable=False)
    role_id = Column(Integer, ForeignKey('roles.id'), primary_key=True, nullable=False)


### COMMENTS

class Discussion(TableBase):
    __tablename__ = 'discussions'
    id = Column(Integer, primary_key=True, nullable=False)
    resource_id = Column(Integer, ForeignKey('resources.id'), nullable=False)
    comment_count = Column(Integer, nullable=False, default=0)

class Comment(TableBase):
    __tablename__ = 'comments'
    id = Column(Integer, primary_key=True, nullable=False)
    discussion_id = Column(Integer, ForeignKey('discussions.id'), nullable=False)
    posted_time = Column(TZDateTime, nullable=False, index=True, default=now)
    author_user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    # Nested set threading; Google it
    left = Column(Integer, index=True, nullable=False)
    right = Column(Integer, index=True, nullable=False)
    content = Column(UnicodeText(4096), nullable=False)

    @property
    def ancestors_query(self):
        """Returns a query that will fetch all comments somewhere above this
        one, in correct linear order.
        """
        # Ancestors are any comments whose left and right contain this
        # comment's left
        return object_session(self).query(Comment) \
            .with_parent(self.discussion) \
            .filter(Comment.left < self.left) \
            .filter(Comment.right > self.right) \
            .order_by(Comment.left.asc())

    @property
    def descendants_query(self):
        """Returns a query that will fetch all comments nested below this one,
        including this one itself, in correct linear order.
        """
        # Descendants are any comments with a left (or right) between
        # comment.left and comment.right
        return object_session(self).query(Comment) \
            .with_parent(self.discussion) \
            .filter(Comment.left.between(self.left, self.right)) \
            .order_by(Comment.left.asc())


### PROFILES

class UserProfile(TableBase):
    __tablename__ = 'user_profiles'
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True, nullable=False)
    content = Column(Unicode, nullable=True)

class UserProfileRevision(TableBase):
    __tablename__ = 'user_profile_revisions'
    user_id = Column(Integer, ForeignKey('users.id'), primary_key=True, nullable=False)
    updated_at = Column(DateTime, primary_key=True, nullable=False, default=now)
    updated_by_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    content = Column(Unicode, nullable=True)


### CERTIFICATES

class Certificate(TableBase):
    """A store for client certificates.  Effectively a CA database.

    To work, it requires a CA certificate and key to be passed in on
    instantiation.  See floof.lib.auth

    """
    __tablename__ = 'certificates'
    id = Column(Integer, primary_key=True, nullable=False)
    serial = Column(Unicode, unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'))
    created_time = Column(TZDateTime, nullable=False)
    expiry_time = Column(TZDateTime, nullable=False)
    revoked = Column(Boolean, index=True, nullable=False, default=False)
    revoked_time = Column(TZDateTime)
    bits = Column(Integer, nullable=False)
    public_data = Column(String, nullable=False)

    @validates('revoked')
    def validate_revoked(self, key, status):
        if self.revoked:
            assert status == True, 'It is not possible to un-revoke a certificate.'
        return status

    @validates('user')
    def validate_user(self, key, user):
        """Enforce that the certificate is available only to the user
        to whom it was issued."""
        cert = ssl.load_certificate(ssl.FILETYPE_PEM, self.public_data)
        assert cert.get_subject().commonName == user.name
        return user

    @property
    def details(self):
        """Render the certificate's public component as human-readable text."""
        pubcert = ssl.load_certificate(ssl.FILETYPE_PEM, self.public_data)
        return ssl.dump_certificate(ssl.FILETYPE_TEXT, pubcert)

    @property
    def expired(self):
        """Returns True if the certificate has expired."""
        return self.expiry_time < now()

    @property
    def public_data_der(self):
        """Returns the DER-encoded from of the certificate (default is PEM)."""
        pubcert = ssl.load_certificate(ssl.FILETYPE_PEM, self.public_data)
        return ssl.dump_certificate(ssl.FILETYPE_ASN1, pubcert)

    @property
    def valid(self):
        """Returns True if the certificate is neither expired nor revoked."""
        return not self.expired and not self.revoked

    class InvalidSPKACError(Exception): pass

    def __init__(self, user, ca_cert, ca_key, spkac=None, bits=2048, days=3653, digest='sha256'):
        """Creates a new certificate for ``user``, signed by the passed CA.

        ``spkac``, if supplied, should be a string representing a UA's
        SPKAC public key as generated by the <keygen> HTML element.
        When present, it is used as the public key, else a public key with
        length ``bits`` is generated.

        """
        if spkac:
            # Strip all whitespace
            spkac = str(spkac)
            spkac = spkac.translate(None, string.whitespace)
            try:
                cert_key = ssl.NetscapeSPKI(spkac).get_pubkey()
            except ssl.Error:
                raise Certificate.InvalidSPKACError
            bits = cert_key.bits()
        else:
            cert_key = ssl.PKey()
            cert_key.generate_key(ssl.TYPE_RSA, bits)

        now = datetime.datetime.now(pytz.utc)
        expire = now + datetime.timedelta(days=days)

        # Uniqueness should be supplied by the user name and the certificate
        # number.  The rand element is mostly for ease of development, to
        # protect old CRLs from clobbering new certs, etc.
        rand = str(random.getrandbits(80))
        hasher = hashlib.sha1(user.name + str(len(user.certificates)) + rand)

        cert = ssl.X509()
        cert.set_version(2)  # Value 2 means v3
        cert.set_serial_number(long(hasher.hexdigest(), 16))
        cert.get_subject().organizationName = ca_cert.get_subject().O
        cert.get_subject().OU = 'Users'
        cert.get_subject().commonName = user.name
        cert.set_notBefore(now.strftime('%Y%m%d%H%M%SZ'))
        cert.set_notAfter(expire.strftime('%Y%m%d%H%M%SZ'))
        cert.set_issuer(ca_cert.get_subject())
        cert.set_pubkey(cert_key)
        cert.add_extensions([
                ssl.X509Extension('authorityKeyIdentifier', False, 'keyid:always,issuer:always', cert, ca_cert),
                ssl.X509Extension('subjectKeyIdentifier', False, 'hash', cert),
                ssl.X509Extension('basicConstraints', True, 'CA:FALSE'),
                ssl.X509Extension('keyUsage', True, 'digitalSignature'),
                ssl.X509Extension('extendedKeyUsage', True, 'clientAuth'),
                ])
        cert.sign(ca_key, digest)

        # The serial must be 40 chars long
        self.serial = u'{0:0>40x}'.format(cert.get_serial_number())
        self.created_time = now
        self.expiry_time = expire
        self.bits = bits
        self.public_data = ssl.dump_certificate(ssl.FILETYPE_PEM, cert).decode()
        if not spkac:
            self._key = cert_key

    def pkcs12(self, passphrase, name, ca_cert, ca_key):
        """Returns a PKCS12 file including the certificate, CA and private key.

        This is only available on a freshly minted certificate (i.e. not
        on a certificate retrieved from the database).

        """
        if not hasattr(self, '_key') or not self._key:
            raise NameError('Certificate private data and hence PKCS12 '
            'files are only available on freshly created Certificate '
            'objects as private keys are not retained in the database.')

        cert = ssl.load_certificate(ssl.FILETYPE_PEM, self.public_data)
        pkcs12 = ssl.PKCS12()
        pkcs12.set_certificate(cert)
        pkcs12.set_privatekey(self._key)
        pkcs12.set_ca_certificates([ca_cert])
        pkcs12.set_friendlyname(str(name))

        return pkcs12.export(passphrase)

    def revoke(self):
        self.revoked = True
        self.revoked_time = now()

    @classmethod
    def get(cls, session, id=None, serial=None):
        try:
            if id is not None:
                return session.query(Certificate).filter_by(id=id).one()
            if serial is not None:
                if isinstance(serial, int):
                    serial = '{0:x}'.format(serial)
                else:
                    serial = unicode(serial.lower())
                return session.query(Certificate).filter_by(serial=serial).one()
        except NoResultFound:
            return None


### TAGS

def get_or_create_tag(name):
    try:
        return session.query(Tag).filter_by(name=name).one()
    except NoResultFound:
        return Tag(name)

class Tag(TableBase):
    __tablename__ = 'tags'
    id = Column(Integer, primary_key=True)
    name = Column(Unicode(64), unique=True)

    def __init__(self, name):
        self.name = name

class Album(TableBase):
    __tablename__ = 'albums'
    id = Column(Integer, primary_key=True)
    name = Column(Unicode(64), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'))
    encapsulation = Column(Enum(u'public', u'private', name='albums_encapsulation'), nullable=False)

artwork_tags = Table('artwork_tags', TableBase.metadata,
    Column('artwork_id', Integer, ForeignKey('artwork.id'), primary_key=True),
    Column('tag_id', Integer, ForeignKey('tags.id'), primary_key=True),
)

artwork_albums = Table('artwork_albums', TableBase.metadata,
    Column('artwork_id', Integer, ForeignKey('artwork.id'), primary_key=True),
    Column('album_id', Integer, ForeignKey('albums.id'), primary_key=True),
)


### Logging

class Log(TableBase):
    __tablename__ = 'logs'
    id = Column(Integer, primary_key=True)
    timestamp = Column(TZDateTime, nullable=False, index=True, default=now)
    logger = Column(String, nullable=False)
    level = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    privileges = Column(Unicode)
    url = Column(Unicode)
    ipaddr = Column(IPAddr)
    target_user_id = Column(Integer, ForeignKey('users.id'))
    message = Column(Unicode, nullable=False)
    reason = Column(Unicode)

    __mapper_args__ = {'order_by': timestamp.desc()}


### RELATIONS
# TODO: For user/user and user/art relations, it would be nice to have SQLA represent them as a dict of lists.
# See: http://www.sqlalchemy.org/docs/orm/collections.html#instrumentation-and-custom-types

make_resource_type(User)
make_resource_type(Artwork)

# Users
User.identity_urls = relation(
    IdentityURL, innerjoin=True, backref='user', cascade="all,delete-orphan")
User.identity_emails = relation(
    IdentityEmail, innerjoin=True, backref='user', cascade="all,delete-orphan")

User.watches = relation(UserWatch,
    primaryjoin=User.id==UserWatch.user_id,
    backref=backref('user', innerjoin=True))
User.inverse_watches = relation(UserWatch,
    primaryjoin=User.id==UserWatch.other_user_id,
    backref=backref('other_user', innerjoin=True))


# Profiles
UserProfile.user = relation(User, innerjoin=True, backref=backref('_profile', uselist=False))
UserProfileRevision.user = relation(User, innerjoin=True,
    foreign_keys=[UserProfileRevision.user_id],
    primaryjoin=UserProfileRevision.user_id == User.id,
    backref='profile_revisions')
UserProfileRevision.updated_by = relation(User, innerjoin=True,
    foreign_keys=[UserProfileRevision.updated_by_id],
    primaryjoin=UserProfileRevision.updated_by_id == User.id)


# Art
#Artwork.discussion = relation(Discussion, backref='artwork')
Artwork.tag_objs = relation(Tag, secondary=artwork_tags, backref=backref('artwork', innerjoin=True))
Artwork.tags = association_proxy('tag_objs', 'name', creator=get_or_create_tag)
Artwork.uploader = relation(User, innerjoin=True,
    backref='uploaded_artwork')
Artwork.user_artwork = relation(UserArtwork,
    backref=backref('artwork', innerjoin=True))
Artwork.ratings = relation(ArtworkRating,
    backref=backref('artwork', innerjoin=True),
    extension=ArtworkRatingsAttributeExtension())

#User.discussion = relation(Discussion, backref='user')
User.user_artwork = relation(UserArtwork, backref=backref('user', innerjoin=True))
User.ratings_given = relation(ArtworkRating, backref=backref('user', innerjoin=True))

# Permissions
User.roles = relation(Role, UserRole.__table__, lazy='joined')

# Comments
Resource.discussion = relation(Discussion, uselist=False,
    backref=backref('resource', innerjoin=True))

Comment.author = relation(User, innerjoin=True, backref='comments')

Discussion.comments = relation(Comment, order_by=Comment.left.asc(),
    backref=backref('discussion', innerjoin=True))

# Certificates
Certificate.user = relation(User, innerjoin=True, backref='certificates')

# Tags & albums
Album.user = relation(User, innerjoin=True, backref='albums')
Album.artwork = relation(Artwork, secondary=artwork_albums, backref='albums')

# Logs
Log.user = relation(User, backref='logs',
        primaryjoin=User.id==Log.user_id,
        order_by=Log.timestamp.desc(),
        )
Log.target_user = relation(User,
        primaryjoin=User.id==Log.target_user_id,
        )
