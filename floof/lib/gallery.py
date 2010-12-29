"""Shared gallery handling.

Intended to be used and usable from basically all over the place.  You probably
want the `GalleryView` class.
"""
from sqlalchemy.orm import aliased
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.sql import and_, case, or_

from floof import model

class GalleryView(object):
    """Represents a view of an art gallery.

    The definition of 'art gallery' is extremely subject to interpretation.  It
    may actually be the art owned by a single user, or it may be a tag or a
    label, or any combination thereof.  Who knows!
    """
    def __init__(self, session=None):
        """Attempts"""
        if not session:
            session = model.meta.Session

        self.session = session
        self.query = session.query(model.Artwork)


    ### Methods for building the query

    def filter_by_user(self, rel, user):
        """Filter the gallery by a user relationship: by/for/of.
        """
        self.query = self.query.filter(
            model.Artwork.user_artwork.any(
                relationship_type=rel,
                user_id=user.id,
            )
        )

    def filter_by_tag(self, tag):
        """Filter the gallery by a named tag.  It may be a regular tag 'foo',
        or a special tag like 'by:foo'.
        """
        if ' ' in tag:
            raise ValueError("Tags cannot contain spaces; is this a list of tags?")

        if tag.startswith(('by:', 'for:', 'of:')):
            # Special user tag
            relation, _, username = tag.partition(':')
            try:
                user = self.session.query(model.User).filter_by(name=username).one()
            except NoResultFound:
                # XXX Do something better??
                raise

            self.filter_by_user(relation, user)

        else:
            # Regular tag
            try:
                tag = self.session.query(model.Tag).filter_by(name=tag).one()
            except NoResultFound:
                # XXX
                raise

            self.query = self.query.filter(
                model.Artwork.tag_objs.any(id=tag.id)
            )

    def filter_by_watches(self, user):
        """Filter the gallery down to only things `user` is watching."""
        # XXX make this work for multiple users
        self.query = self.query.filter(or_(
            # Check for by/for/of watching
            # XXX need an index on relationship_type, badly!
            model.Artwork.id.in_(
                self.session.query(model.UserArtwork.artwork_id)
                    .join((model.UserWatch, model.UserArtwork.user_id == model.UserWatch.other_user_id))
                    .filter(model.UserWatch.user_id == user.id)
                    .filter(case(
                        value=model.UserArtwork.relationship_type,
                        whens={
                            u'by': model.UserWatch.watch_by,
                            u'for': model.UserWatch.watch_for,
                            u'of': model.UserWatch.watch_of,
                        },
                    ))
            ),
            # Check for upload watching
            model.Artwork.uploader_user_id.in_(
                self.session.query(model.UserWatch.other_user_id)
                    .filter(model.UserWatch.user_id == user.id)
                    .filter(model.UserWatch.watch_upload == True)  # gross
            ),
        ))


    ### Methods for examining the result

    def get_query(self):
        """Get the constructed query, sorted in the usual way: by uploaded
        time, most recent first.
        """
        return self.query.order_by(model.Artwork.uploaded_time.desc())