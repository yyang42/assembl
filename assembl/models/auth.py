from datetime import datetime
from itertools import chain
import urllib
import hashlib
import chroma
import simplejson as json
from collections import defaultdict

from sqlalchemy import (
    Boolean,
    Column,
    String,
    ForeignKey,
    Integer,
    UnicodeText,
    DateTime,
    Time,
    Binary,
    desc,
    Index
)

from pyramid.httpexceptions import (
    HTTPUnauthorized, HTTPBadRequest)
from sqlalchemy.orm import relationship, backref, deferred
from sqlalchemy import inspect
from sqlalchemy.types import Text
from sqlalchemy.schema import Index
from sqlalchemy.orm.attributes import NO_VALUE
from pyramid.security import Everyone, Authenticated
from virtuoso.vmapping import IriClass
from virtuoso.alchemy import CoerceUnicode

from ..lib import config
from ..lib.sqla import (UPDATE_OP, INSERT_OP, get_model_watcher)
from . import Base, DiscussionBoundBase, DiscussionBoundTombstone
from ..auth import *
from ..semantic.namespaces import (
    SIOC, ASSEMBL, CATALYST, QUADNAMES, VERSION, FOAF, DCTERMS, RDF, VirtRDF)
from ..semantic.virtuoso_mapping import QuadMapPatternS, USER_SECTION


class AgentProfile(Base):
    """
    An agent could be a person, group, bot or computer.
    Profiles describe agents, which have multiple accounts.
    Some agents might also be users of the platforms.
    """
    __tablename__ = "agent_profile"
    rdf_class = FOAF.Agent
    rdf_sections = (USER_SECTION,)

    id = Column(Integer, primary_key=True,
        info={'rdf': QuadMapPatternS(None, ASSEMBL.db_id)})
    name = Column(CoerceUnicode(1024),
        info={'rdf': QuadMapPatternS(None, FOAF.name)})
    description = Column(UnicodeText,
        info={'rdf': QuadMapPatternS(None, DCTERMS.description)})
    type = Column(String(60))

    __mapper_args__ = {
        'polymorphic_identity': 'agent_profile',
        'polymorphic_on': type,
        'with_polymorphic': '*'
    }

    def get_preferred_email_account(self):
        if inspect(self).attrs.accounts.loaded_value is NO_VALUE:
            # TODO: remove polymorphism
            account = self.db.query(AbstractAgentAccount).filter(
                (AbstractAgentAccount.profile_id == self.id)
                & (AbstractAgentAccount.email != None)).order_by(
                AbstractAgentAccount.verified.desc(),
                AbstractAgentAccount.preferred.desc()).first()
            if account:
                return account
        elif self.accounts:
            accounts = [a for a in self.accounts if a.email]
            accounts.sort(key=lambda e: (not e.verified, not e.preferred))
            return accounts[0]

    def get_preferred_email(self):
        if self.get_preferred_email_account() is not None:
            return self.get_preferred_email_account().email

    def real_name(self):
        if not self.name:
            for acc in self.identity_accounts:
                name = acc.real_name()
                if name:
                    self.name = name
                    break
        return self.name

    def display_name(self):
        # TODO: Prefer types?
        if self.name:
            return self.name
        for acc in self.identity_accounts:
            if acc.username:
                return acc.display_name()
        for acc in self.accounts:
            name = acc.display_name()
            if name:
                return name

    def merge(self, other_profile):
        session = self.db
        assert not (
            isinstance(other_profile, User) and not isinstance(self, User))
        my_accounts = {a.signature(): a for a in self.accounts}
        for other_account in other_profile.accounts:
            my_account = my_accounts.get(other_account.signature())
            if my_account:
                my_account.merge(other_account)
                session.delete(other_account)
            else:
                other_account.profile = self
        if other_profile.name and not self.name:
            self.name = other_profile.name
        # TODO: similarly for posts
        from .action import Action
        for action in session.query(Action).filter_by(
            actor_id=other_profile.id).all():
                action.actor = self

    def has_permission(self, verb, subject):
        if self is subject.owner:
            return True

        return self.db.query(Permission).filter_by(
            actor_id=self.id,
            subject_id=subject.id,
            verb=verb,
            allow=True
        ).one()

    def avatar_url(self, size=32, app_url=None, email=None):
        default = config.get('avatar.default_image_url') or \
            (app_url and app_url+'/static/img/icon/user.png')

        offline_mode = config.get('offline_mode')
        if offline_mode == "true":
            return default

        for acc in self.identity_accounts:
            url = acc.avatar_url(size)
            if url:
                return url
        # Otherwise: Use the gravatar URL
        email = email or self.get_preferred_email()
        if not email:
            return default
        default = config.get('avatar.gravatar_default') or default
        return EmailAccount.avatar_url_for(email, size, default)

    def external_avatar_url(self):
        return "/user/id/%d/avatar/" % (self.id,)

    def serializable(self, use_email=None):
        # Obsolete method. We want to switch to view_defs.
        # Not returning the email is intentional for confidentiality reasons
        return {
            '@type': self.external_typename(),
            '@id': self.uri_generic(self.id),
            'name': self.name or self.display_name()
        }

    def get_agent_preload(self, view_def=None):
        if view_def:
            result = self.generic_json(view_def, user_id=self.id)
        else:
            result = self.serializable()
        return json.dumps(result)

    def count_posts_in_discussion(self, discussion_id):
        from .post import Post
        return self.db.query(Post).filter_by(
            creator_id=self.id, discussion_id=discussion_id).count()

    def count_posts_in_current_discussion(self):
        "CAN ONLY BE CALLED FROM API V2"
        from ..auth.util import get_current_discussion
        discussion = get_current_discussion()
        if discussion is None:
            return None
        return self.count_posts_in_discussion(discussion.id)

    def get_preferred_locale(self):
        # TODO: per-user preferred locale
        return None


class AbstractAgentAccount(Base):
    """An abstract class for accounts that identify agents"""
    __tablename__ = "abstract_agent_account"
    rdf_class = SIOC.UserAccount
    rdf_sections = (USER_SECTION,)

    id = Column(Integer, primary_key=True,
                info={'rdf': QuadMapPatternS(None, ASSEMBL.db_id)})

    type = Column(String(60))

    profile_id = Column(
        Integer,
        ForeignKey('agent_profile.id', ondelete='CASCADE', onupdate='CASCADE'),
        nullable=False,
        info={'rdf': QuadMapPatternS(None, SIOC.account_of)})

    profile = relationship('AgentProfile', backref=backref(
        'accounts', cascade="all, delete-orphan"))

    preferred = Column(Boolean(), default=False, server_default='0')
    verified = Column(Boolean(), default=False, server_default='0')
    # Note some social accounts don't disclose email (eg twitter), so nullable
    # Virtuoso + nullable -> no unique index (sigh)
    email = Column(String(100), index=True)
    #    info={'rdf': QuadMapPatternS(None, SIOC.email)} private

    def signature(self):
        "Identity of signature implies identity of underlying account"
        return ('abstract_agent_account', self.id)

    def merge(self, other):
        pass

    def is_owner(self, user_id):
        return self.profile_id == user_id

    @classmethod
    def restrict_to_owners(cls, query, user_id):
        "filter query according to object owners"
        return query.filter(cls.profile_id == user_id)

    __mapper_args__ = {
        'polymorphic_identity': 'abstract_agent_account',
        'polymorphic_on': type,
        'with_polymorphic': '*'
    }

    crud_permissions = CrudPermissions(
        P_READ, P_SYSADMIN, P_SYSADMIN, P_SYSADMIN,
        P_READ, P_READ, P_READ)

    @classmethod
    def user_can_cls(cls, user_id, operation, permissions):
        s = super(AbstractAgentAccount, cls).user_can_cls(
            user_id, operation, permissions)
        return IF_OWNED if s is False else s

    def user_can(self, user_id, operation, permissions):
        # bypass for permission-less new users
        if user_id == self.profile_id:
            return True
        return super(AbstractAgentAccount, self).user_can(
            user_id, operation, permissions)


class EmailAccount(AbstractAgentAccount):
    """An email account"""
    __mapper_args__ = {
        'polymorphic_identity': 'agent_email_account',
    }
    profile_e = relationship(AgentProfile, backref=backref('email_accounts'))

    def display_name(self):
        if self.verified:
            return self.email

    def serialize_profile(self):
        # Obsolete method. We want to switch to view_defs.
        return self.profile.serializable(self.email)

    def signature(self):
        return ('agent_email_account', self.email,)

    def merge(self, other):
        if other.verified:
            self.verified = True

    def other_account(self):
        if not self.verified:
            return self.db.query(self.__class__).filter_by(
                email=self.email, verified=True).first()

    def avatar_url(self, size=32, default=None):
        return self.avatar_url_for(self.email, size, default)

    @staticmethod
    def avatar_url_for(email, size=32, default=None):
        args = {'s': str(size)}
        if default:
            args['d'] = default
        return "//www.gravatar.com/avatar/%s?%s" % (
            hashlib.md5(email.lower()).hexdigest(), urllib.urlencode(args))

    @staticmethod
    def get_or_make_profile(session, email, name=None):
        emails = list(session.query(EmailAccount).filter_by(
            email=email).all())
        # We do not want unverified user emails
        # This is costly. I should have proper boolean markers
        emails = [e for e in emails if e.verified or not isinstance(e.profile, User)]
        user_emails = [e for e in emails if isinstance(e.profile, User)]
        if user_emails:
            assert len(user_emails) == 1
            return user_emails[0]
        elif emails:
            # should also be 1 but less confident.
            return emails[0]
        else:
            profile = AgentProfile(name=name)
            emailAccount = EmailAccount(email=email, profile=profile)
            session.add(emailAccount)
            return emailAccount


class IdentityProvider(Base):
    """An identity provider (or sometimes a category of identity providers.)"""
    __tablename__ = "identity_provider"
    rdf_class = SIOC.Usergroup
    rdf_sections = (USER_SECTION,)

    id = Column(Integer, primary_key=True)
    provider_type = Column(String(20), nullable=False)
    name = Column(String(60), nullable=False,
        info={'rdf': QuadMapPatternS(None, SIOC.name)})
    # TODO: More complicated model, where trust also depends on realm.
    trust_emails = Column(Boolean, default=False)


class IdentityProviderAccount(AbstractAgentAccount):
    """An account with an external identity provider"""
    __tablename__ = "idprovider_agent_account"
    __mapper_args__ = {
        'polymorphic_identity': 'idprovider_agent_account',
    }
    id = Column(Integer, ForeignKey(
        'abstract_agent_account.id',
        ondelete='CASCADE', onupdate='CASCADE'
    ), primary_key=True)
    provider_id = Column(
        Integer,
        ForeignKey('identity_provider.id', ondelete='CASCADE', onupdate='CASCADE'),
        nullable=False,
        info={'rdf': QuadMapPatternS(None, SIOC.member_of)})
    provider = relationship(IdentityProvider)
    username = Column(String(200),
        info={'rdf': QuadMapPatternS(None, SIOC.name)})
    domain = Column(String(200))
    userid = Column(String(200),
        info={'rdf': QuadMapPatternS(None, SIOC.id)})
    profile_info = deferred(Column(Text()))
    picture_url = Column(String(300),
        info={'rdf': QuadMapPatternS(None, FOAF.img)})
    profile_i = relationship(AgentProfile, backref='identity_accounts')

    def __init__(self, profile_info_json=None, **kwargs):
        if profile_info_json is not None:
            kwargs['profile_info'] = json.dumps(profile_info_json)
        super(IdentityProviderAccount, self).__init__(**kwargs)
        self.interpret_profile(self.profile_info_json)

    def signature(self):
        return ('idprovider_agent_account', self.provider_id, self.username,
                self.domain, self.userid)

    def interpret_profile(self, profile=None):
        profile = profile or self.profile_info_json
        if not profile:
            return
        self.populate_picture(profile)
        email = profile.get('verifiedEmail', self.email)
        if email and email != self.email:
            self.email = email
            self.verified = self.provider.trust_emails

    def display_name(self):
        # TODO: format according to provider, ie @ for twitter.
        if self.username:
            name = self.username
        else:
            name = self.userid
        return ":".join((self.provider.provider_type, name))

    def get_provider_name(self):
        return self.provider.name

    def real_name(self):
        info = self.profile_info_json
        name = info['name']
        if name.get('formatted', None):
            return name['formatted']
        if 'givenName' in name and 'familyName' in name:
            return ' '.join((name['givenName'], name['familyName']))

    def populate_picture(self, profile):
        if 'photos' in profile:  # google, facebook
            photos = [x.get('value', None) for x in profile['photos']]
            photos = [x for x in photos if x]
            if photos:
                self.picture_url = photos[0]
        elif self.provider.provider_type == 'facebook':
            accounts = [x.get('userid') for x in profile.get('accounts', ())]
            accounts = [x for x in accounts if x]
            if accounts:
                self.picture_url = 'http://graph.facebook.com/%s/picture' % (
                    accounts[0])

    facebook_sizes = (('square', 50), ('small', 50), ('normal', 100), ('large', 200))
    twitter_sizes = (('_mini', 25), ('_normal', 48), ('_bigger', 73), ('', 1000))

    def avatar_url(self, size=32):
        if not self.picture_url:
            return
        if self.provider.provider_type == 'google_oauth2':
            return '%s?size=%d' % (self.picture_url, size)
        elif self.provider.provider_type == 'facebook':
            for (size_name, name_size) in self.facebook_sizes:
                if size <= name_size:
                    break
            return '%s?type=%s' % (self.picture_url, size_name)
        elif self.provider.provider_type == 'twitter':
            for (size_name, name_size) in self.twitter_sizes:
                if size <= name_size:
                    break
            return size_name.join(self.picture_url.split('_normal'))

    @property
    def profile_info_json(self):
        if self.profile_info:
            return json.loads(self.profile_info)
        return {}

    @profile_info_json.setter
    def profile_info_json(self, val):
        self.profile_info = json.dumps(val)
        self.interpret_profile(val)



class User(AgentProfile):
    """
    A Human user.
    """
    __tablename__ = "user"

    __mapper_args__ = {
        'polymorphic_identity': 'user'
    }

    id = Column(
        Integer,
        ForeignKey('agent_profile.id', ondelete='CASCADE', onupdate='CASCADE'),
        primary_key=True
    )

    preferred_email = Column(CoerceUnicode(50),
        info={'rdf': QuadMapPatternS(None, FOAF.mbox)})
    verified = Column(Boolean(), default=False)
    password = deferred(Column(Binary(115)))
    timezone = Column(Time(True))
    last_login = Column(DateTime)
    login_failures = Column(Integer, default=0)
    creation_date = Column(DateTime, nullable=False, default=datetime.utcnow,
        info={'rdf': QuadMapPatternS(None, DCTERMS.created)})

    def __init__(self, **kwargs):
        if kwargs.get('password'):
            from ..auth.password import hash_password
            kwargs['password'] = hash_password(kwargs['password'])

        super(User, self).__init__(**kwargs)

    @property
    def username_p(self):
        if self.username:
            return self.username.username

    @username_p.setter
    def username_p(self, name):
        if self.username:
            self.username.username = name
        else:
            self.username = Username(username=name)

    @username_p.deleter
    def username_p(self):
        if self.username:
            self.db.delete(self.username)

    def set_password(self, password):
        from ..auth.password import hash_password
        self.password = hash_password(password)

    def check_password(self, password):
        if self.password:
            from ..auth.password import verify_password
            return verify_password(password, self.password)
        return False

    def get_preferred_email(self):
        if self.preferred_email:
            return self.preferred_email
        return super(User, self).get_preferred_email()

    def merge(self, other_user):
        super(User, self).merge(other_user)
        if isinstance(other_user, User):
            session = self.db
            if other_user.preferred_email and not self.preferred_email:
                self.preferred_email = other_user.preferred_email
            if other_user.last_login:
                if self.last_login:
                    self.last_login = max(
                        self.last_login, other_user.last_login)
                else:
                    self.last_login = other_user.last_login
            self.creation_date = min(
                self.creation_date, other_user.creation_date)
            if other_user.password and not self.password:
                # NOTE: The user may be confused by the implicit change of
                # password when we destroy the second account.
                # Use most recent login
                if other_user.last_login > self.last_login:
                    self.password = other_user.password
            for extract in other_user.extracts_created:
                extract.creator = self
            for extract in other_user.extracts_owned:
                extract.owner = self
            for post in other_user.posts_created:
                post.creator = self
            for role in other_user.roles:
                role.user = self
            for role in other_user.local_roles:
                role.user = self
            if other_user.username and not self.username:
                self.username = other_user.username
            for notification_subscription in \
                    other_user.notification_subscriptions:
                notification_subscription.user = self
                if not notification_subscription.check_unique():
                    self.db.delete(notification_subscription)

    def send_email(self, **kwargs):
        subject = kwargs.get('subject', '')
        body = kwargs.get('body', '')

        # Send email.

    def avatar_url(self, size=32, app_url=None, email=None):
        return super(User, self).avatar_url(
            size, app_url, email or self.preferred_email)

    def display_name(self):
        if self.username:
            return self.username.username
        return super(User, self).display_name()

    def __repr__(self):
        if self.username:
            return "<User '%s'>" % self.username.username.encode('utf-8')
        else:
            return "<User id=%d>" % self.id

    def get_permissions(self, discussion_id):
        from ..auth.util import get_permissions
        return get_permissions(self.id, discussion_id)

    def get_all_permissions(self):
        from ..auth.util import get_permissions
        from .discussion import Discussion
        permissions = {
            Discussion.uri_generic(d_id): get_permissions(self.id, d_id)
            for (d_id,) in self.db.query(Discussion.id)}
        return permissions

    def send_to_changes(self, connection=None, operation=UPDATE_OP):
        super(User, self).send_to_changes(connection, operation)
        watcher = get_model_watcher()
        if operation == UPDATE_OP:
            watcher.processAccountModified(self.id)
        elif operation == INSERT_OP:
            watcher.processAccountCreated(self.id)

    def serializable(self, use_email=None):
        # Obsolete method. We want to switch to view_defs.
        ser = super(User, self).serializable()
        ser['username'] = self.display_name()
        #r['email'] = use_email or self.get_preferred_email()
        return ser

    def subscribe(self, discussion, role=R_PARTICIPANT):
        existing = self.db.query(LocalUserRole).join(Role).filter(
            LocalUserRole.user_id == self.id,
            Role.name == role,
            LocalUserRole.discussion_id == discussion.id).first()
        if not existing:
            role = self.db.query(Role).filter_by(name=role).one()
            self.db.add(LocalUserRole(
                user=self, role=role, discussion=discussion))

    def unsubscribe(self, discussion, role=R_PARTICIPANT):
        existing = self.db.query(LocalUserRole).join(Role).filter(
            LocalUserRole.user_id == self.id,
            Role.name == role,
            LocalUserRole.discussion_id == discussion.id).all()
        print "ZZZZ"
        print repr(existing)
        if existing:
            for lur in existing:
                print "deleting" + repr(lur)
                self.db.delete(lur)

    @classmethod
    def extra_collections(cls):
        from assembl.views.traversal import CollectionDefinition
        from .notification import NotificationSubscription
        from .discussion import Discussion
        class NotificationSubscriptionCollection(CollectionDefinition):
            def __init__(self, cls):
                super(NotificationSubscriptionCollection, self).__init__(
                    cls, User.notification_subscriptions.property)

            def decorate_query(self, query, last_alias, parent_instance, ctx):

                query = super(
                    NotificationSubscriptionCollection, self).decorate_query(
                    query, last_alias, parent_instance, ctx)
                discussion = ctx.get_instance_of_class(Discussion)
                if discussion is not None:
                    # Materialize active subscriptions... TODO: Make this batch,
                    # also dematerialize
                    if isinstance(parent_instance, UserTemplate):
                        parent_instance.get_notification_subscriptions()
                    else:
                        parent_instance.get_notification_subscriptions(discussion.id)
                    query = query.filter(last_alias.discussion_id == discussion.id)
                return query

            def decorate_instance(
                    self, instance, parent_instance, assocs, user_id,
                    ctx, kwargs):
                super(NotificationSubscriptionCollection,
                      self).decorate_instance(instance, parent_instance, assocs, user_id,
                    ctx, kwargs)

            def contains(self, parent_instance, instance):
                if not super(NotificationSubscriptionCollection, self).contains(
                        parent_instance, instance):
                    return False
                # Don't I need the context to get the discussion? Rats!
                return True

            def get_default_view(self):
                return "extended"

        class LocalRoleCollection(CollectionDefinition):
            def __init__(self, cls):
                super(LocalRoleCollection, self).__init__(
                    cls, User.local_roles.property)

            def decorate_query(self, query, last_alias, parent_instance, ctx):

                query = super(
                    LocalRoleCollection, self).decorate_query(
                    query, last_alias, parent_instance, ctx)
                discussion = ctx.get_instance_of_class(Discussion)
                if discussion is not None:
                    query = query.filter(last_alias.discussion_id == discussion.id)
                return query

            def decorate_instance(
                    self, instance, parent_instance, assocs, user_id,
                    ctx, kwargs):
                super(LocalRoleCollection,
                      self).decorate_instance(instance, parent_instance, assocs, user_id,
                    ctx, kwargs)

            def contains(self, parent_instance, instance):
                if not super(LocalRoleCollection, self).contains(
                        parent_instance, instance):
                    return False
                # Don't I need the context to get the discussion? Rats!
                return True

            def get_default_view(self):
                return "default"

        return {
            'notification_subscriptions': NotificationSubscriptionCollection(cls),
            'local_roles': LocalRoleCollection(cls)}

    def get_notification_subscriptions_for_current_discussion(self):
        "CAN ONLY BE CALLED FROM API V2"
        from ..auth.util import get_current_discussion
        discussion = get_current_discussion()
        if discussion is None:
            return []
        return self.get_notification_subscriptions(discussion.id)

    def get_notification_subscriptions(
            self, discussion_id, reset_defaults=False):
        """the notification subscriptions for this user and discussion.
        Includes materialized subscriptions from the template."""
        from .notification import (
            NotificationSubscription, NotificationSubscriptionStatus, NotificationCreationOrigin)
        from .discussion import Discussion
        from ..auth.util import get_roles
        my_subscriptions = self.db.query(NotificationSubscription).filter_by(
            discussion_id=discussion_id, user_id=self.id).all()
        my_subscriptions_classes = {s.__class__ for s in my_subscriptions}
        needed_classes = UserTemplate.get_applicable_notification_subscriptions_classes()
        missing = set(needed_classes) - my_subscriptions_classes
        if (not missing) and not reset_defaults:
            return my_subscriptions
        discussion = Discussion.get(discussion_id)
        assert discussion
        my_roles = get_roles(self.id, discussion_id)
        subscribed = defaultdict(bool)
        for role in my_roles:
            template, changed = discussion.get_user_template(
                role, role == R_PARTICIPANT)
            if template is None:
                continue
            template_subscriptions = template.get_notification_subscriptions()
            for subscription in template_subscriptions:
                subscribed[subscription.__class__] |= subscription.status == NotificationSubscriptionStatus.ACTIVE
        if reset_defaults:
            for sub in my_subscriptions[:]:
                if (sub.creation_origin == NotificationCreationOrigin.DISCUSSION_DEFAULT
                        and sub.status == NotificationSubscriptionStatus.ACTIVE
                        and sub.__class__ in subscribed  # only actual defaults
                        and not subscribed[sub.__class__]):
                    self.db.delete(sub)
                    my_subscriptions.remove(sub)
                    my_subscriptions_classes.discard(sub.__class__)
            missing = set(needed_classes) - my_subscriptions_classes
        defaults = []
        for cls in missing:
            active = subscribed[cls]
            sub = cls(
                discussion_id=discussion_id,
                user_id=self.id,
                creation_origin=NotificationCreationOrigin.DISCUSSION_DEFAULT,
                status=NotificationSubscriptionStatus.ACTIVE if active else NotificationSubscriptionStatus.INACTIVE_DFT)
            defaults.append(sub)
            if active:
                # materialize
                self.db.add(sub)
        self.db.flush()
        return chain(my_subscriptions, defaults)

    def user_can(self, user_id, operation, permissions):
        # bypass for permission-less new users
        if user_id == self.id:
            return True
        return super(User, self).user_can(user_id, operation, permissions)



class Username(Base):
    """Optional usernames for users
    This is in one-one relationships to users.
    Usernames are unique, and in one-one relationships to users.
    It exists because we cannot have a unique index on a nullable property in virtuoso.
    """
    __tablename__ = 'username'
    user_id = Column(Integer,
                     ForeignKey('user.id', ondelete='CASCADE', onupdate='CASCADE'),
                     unique=True)
    username = Column(CoerceUnicode(20), primary_key=True)
    user = relationship(User, backref=backref('username', uselist=False, lazy="joined"))

    def get_id_as_str(self):
        return str(self.user_id)

    @classmethod
    def special_quad_patterns(cls, alias_maker, discussion_id):
        return [QuadMapPatternS(User.iri_class().apply(Username.user_id),
            SIOC.name, Username.username,
            name=QUADNAMES.class_User_username, sections=(USER_SECTION,))]


class Role(Base):
    """A role that a user may have in a discussion"""
    __tablename__ = 'role'
    rdf_class = SIOC.Role
    rdf_sections = (USER_SECTION,)

    id = Column(Integer, primary_key=True,
        info={'rdf': QuadMapPatternS(None, ASSEMBL.db_id)})
    name = Column(String(20), nullable=False,
        info={'rdf': QuadMapPatternS(None, SIOC.name)})

    @classmethod
    def get_role(cls, session, name):
        return session.query(cls).filter_by(name=name).first()


def populate_default_roles(session):
    roles = {r[0] for r in session.query(Role.name).all()}
    for role in SYSTEM_ROLES - roles:
        session.add(Role(name=role))


class UserRole(Base):
    """roles that a user has globally (eg admin.)"""
    __tablename__ = 'user_role'
    rdf_sections = (USER_SECTION,)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('user.id', ondelete='CASCADE', onupdate='CASCADE'),
                     index=True)
    user = relationship(User, backref=backref("roles", cascade="all, delete-orphan"))
    role_id = Column(Integer, ForeignKey('role.id', ondelete='CASCADE', onupdate='CASCADE'))
    role = relationship(Role)

    @classmethod
    def special_quad_patterns(cls, alias_maker, discussion_id):
        return [
        QuadMapPatternS(User.iri_class().apply(UserRole.user_id),
            SIOC.has_function, Role.iri_class().apply(UserRole.role_id),
            name=QUADNAMES.class_UserRole_global, sections=(USER_SECTION,)),
        QuadMapPatternS(User.iri_class().apply(UserRole.user_id),
                    SIOC.has_function, Role.iri_class().apply(UserRole.role_id),
                    name=QUADNAMES.class_UserRole_local)]


class LocalUserRole(DiscussionBoundBase):
    """The role that a user has in the context of a discussion"""
    __tablename__ = 'local_user_role'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('user.id', ondelete='CASCADE', onupdate='CASCADE'))
    user = relationship(User, backref=backref("local_roles", cascade="all, delete-orphan"))
    discussion_id = Column(Integer, ForeignKey(
        'discussion.id', ondelete='CASCADE'))
    discussion = relationship(
        'Discussion', backref=backref(
            "local_user_roles", cascade="all, delete-orphan"),
        info={'rdf': QuadMapPatternS(None, ASSEMBL.in_conversation)})
    role_id = Column(Integer, ForeignKey('role.id', ondelete='CASCADE', onupdate='CASCADE'))
    role = relationship(Role)
    requested = Column(Boolean, server_default='0', default=False)
    # BUG in virtuoso: It will often refuse to create an index
    # whose name exists in another schema. So having this index in
    # schemas assembl and assembl_test always fails.
    # TODO: Bug virtuoso about this,
    # or introduce the schema name in the index name as workaround.
    # __table_args__ = (
    #     Index('user_discussion_idx', 'user_id', 'discussion_id'),)

    def get_discussion_id(self):
        return self.discussion_id

    @classmethod
    def get_discussion_conditions(cls, discussion_id, alias_maker=None):
        return (cls.id == discussion_id,)

    def get_role_name(self):
        return self.role.name

    def unique_query(self, query):
        query = super(LocalUserRole, self).unique_query(query)
        user_id = self.user_id or self.user.id
        role_id = self.role_id or self.role.id
        discussion_id = self.discussion_id or self.discussion.id
        return query.filter_by(
            user_id=user_id, role_id=role_id, discussion_id=discussion_id), True

    def _do_update_from_json(
            self, json, parse_def, aliases, ctx, permissions,
            user_id, duplicate_error=True):
        # TODO: Verify uniqueness
        json_user_id = json.get('user', None)
        if json_user_id is None:
            json_user_id = user_id
        else:
            json_user_id = User.get_database_id(json_user_id)
            # Do not allow changing user
            if self.user_id is not None and json_user_id != self.user_id:
                raise HTTPBadRequest()
        self.user_id = json_user_id
        role_name = json.get("role", None)
        if not (role_name or self.role_id):
            role_name = R_PARTICIPANT
        if role_name:
            role = self.db.query(Role).filter_by(name=role_name).first()
            if not role:
                raise HTTPBadRequest("Invalid role name:"+role_name)
            self.role = role
        json_discussion_id = json.get('discussion', None)
        if json_discussion_id:
            from .discussion import Discussion
            json_discussion_id = Discussion.get_database_id(json_discussion_id)
            # Do not allow change of discussion
            if self.discussion_id is not None \
                    and json_discussion_id != self.discussion_id:
                raise HTTPBadRequest()
            self.discussion_id = json_discussion_id
        else:
            if not self.discussion_id:
                raise HTTPBadRequest()
        return self

    def is_owner(self, user_id):
        return self.user_id == user_id

    @classmethod
    def restrict_to_owners(cls, query, user_id):
        "filter query according to object owners"
        return query.filter(cls.user_id == user_id)

    @classmethod
    def base_conditions(cls, alias=None, alias_maker=None):
        cls = alias or cls
        return (cls.requested == 0,)

    @classmethod
    def special_quad_patterns(cls, alias_maker, discussion_id):
        return [QuadMapPatternS(User.iri_class().apply(cls.user_id),
            SIOC.has_function, Role.iri_class().apply(cls.role_id),
            conditions=(cls.requested == 0,),
            name=QUADNAMES.class_LocalUserRole)]

    crud_permissions = CrudPermissions(
        P_SELF_REGISTER, P_READ, P_ADMIN_DISC, P_ADMIN_DISC,
        P_SELF_REGISTER, P_SELF_REGISTER)

    @classmethod
    def user_can_cls(cls, user_id, operation, permissions):
        # bypass... more checks are required upstream,
        # see assembl.views.api2.auth.add_local_role
        if operation == CrudPermissions.CREATE \
                and P_SELF_REGISTER_REQUEST in permissions:
            return True
        return super(LocalUserRole, cls).user_can_cls(
            user_id, operation, permissions)


class Permission(Base):
    """A permission that a user may have"""
    __tablename__ = 'permission'
    id = Column(Integer, primary_key=True)
    name = Column(String(20), nullable=False)


def populate_default_permissions(session):
    perms = {p[0] for p in session.query(Permission.name).all()}
    for perm in ASSEMBL_PERMISSIONS - perms:
        session.add(Permission(name=perm))


class DiscussionPermission(DiscussionBoundBase):
    """Which permissions are given to which roles for a given discussion."""
    __tablename__ = 'discussion_permission'
    id = Column(Integer, primary_key=True)
    discussion_id = Column(Integer, ForeignKey(
        'discussion.id', ondelete='CASCADE', onupdate='CASCADE'))
    discussion = relationship(
        'Discussion', backref=backref(
            "acls", cascade="all, delete-orphan"),
        info={'rdf': QuadMapPatternS(None, ASSEMBL.in_conversation)})
    role_id = Column(Integer, ForeignKey('role.id', ondelete='CASCADE', onupdate='CASCADE'))
    role = relationship(Role)
    permission_id = Column(Integer, ForeignKey(
        'permission.id', ondelete='CASCADE', onupdate='CASCADE'))
    permission = relationship(Permission)

    def role_name(self):
        return self.role.name

    def permission_name(self):
        return self.permission.name

    def get_discussion_id(self):
        return self.discussion_id

    @classmethod
    def get_discussion_conditions(cls, discussion_id, alias_maker=None):
        return (cls.id == discussion_id, )


def create_default_permissions(session, discussion):
    permissions = {p.name: p for p in session.query(Permission).all()}
    roles = {r.name: r for r in session.query(Role).all()}

    def add_perm(permission_name, role_names):
        # Note: Must be called within transaction manager
        for role in role_names:
            session.add(DiscussionPermission(
                discussion=discussion, role=roles[role],
                permission=permissions[permission_name]))
    add_perm(P_READ, [Everyone])
    add_perm(P_SELF_REGISTER, [Authenticated])
    add_perm(P_ADD_POST,
             [R_PARTICIPANT, R_CATCHER, R_MODERATOR, R_ADMINISTRATOR])
    add_perm(P_EDIT_POST, [R_MODERATOR, R_ADMINISTRATOR])
    add_perm(P_ADD_EXTRACT,
             [R_CATCHER, R_MODERATOR, R_ADMINISTRATOR])
    add_perm(P_EDIT_EXTRACT, [R_MODERATOR, R_ADMINISTRATOR])
    add_perm(P_EDIT_MY_EXTRACT, [R_CATCHER, R_MODERATOR, R_ADMINISTRATOR])
    add_perm(P_ADD_IDEA, [R_CATCHER, R_MODERATOR, R_ADMINISTRATOR])
    add_perm(P_EDIT_IDEA, [R_CATCHER, R_MODERATOR, R_ADMINISTRATOR])
    add_perm(P_EDIT_SYNTHESIS, [R_MODERATOR, R_ADMINISTRATOR])
    add_perm(P_SEND_SYNTHESIS, [R_MODERATOR, R_ADMINISTRATOR])
    add_perm(P_ADMIN_DISC, [R_ADMINISTRATOR])
    add_perm(P_SYSADMIN, [R_ADMINISTRATOR])


class UserTemplate(DiscussionBoundBase, User):
    "A fake user with default permissions and Notification Subscriptions."
    __tablename__ = "user_template"

    __mapper_args__ = {
        'polymorphic_identity': 'user_template'
    }

    id = Column(
        Integer,
        ForeignKey('user.id', ondelete='CASCADE', onupdate='CASCADE'),
        primary_key=True
    )

    discussion_id = Column(Integer, ForeignKey(
        "discussion.id", ondelete='CASCADE', onupdate='CASCADE'))
    discussion = relationship(
        "Discussion", backref=backref(
            "user_templates", cascade="all, delete-orphan"),
        info={'rdf': QuadMapPatternS(None, ASSEMBL.in_conversation)})

    role_id = Column(Integer, ForeignKey(
        Role.id, ondelete='CASCADE', onupdate='CASCADE'))
    for_role = relationship(Role)

    # Create an index for (discussion, role)?

    def get_discussion_id(self):
        return self.discussion_id

    @classmethod
    def get_discussion_conditions(cls, discussion_id, alias_maker=None):
        return (cls.discussion_id == discussion_id,)

    @classmethod
    def get_applicable_notification_subscriptions_classes(cls):
        """
        The classes of notifications subscriptions that make sense to put in 
        a template user.
        
        Right now, that is all concrete classes that are global to the discussion.
        """
        from ..lib.utils import get_concrete_subclasses_recursive
        from ..models import NotificationSubscriptionGlobal
        return get_concrete_subclasses_recursive(NotificationSubscriptionGlobal)

    def get_notification_subscriptions(self):
        return self.get_notification_subscriptions_and_changed()[0]

    def get_notification_subscriptions_and_changed(self):
        """the notification subscriptions for this template.
        Materializes applicable subscriptions.."""
        from .notification import (
            NotificationSubscription,
            NotificationSubscriptionStatus,
            NotificationCreationOrigin)
        # self.id may not be defined
        self.db.flush()
        needed_classes = \
            self.get_applicable_notification_subscriptions_classes()
        # We need to materialize missing NotificationSubscriptions,
        # But have duplication issues, probably due to calls on multiple
        # threads.
        # TEMPORARY: We will apply a write lock selectively.
        # LONG TERM: We will only materialize subscriptions when selected.

        def get_subcriptions(lock):
            query = self.db.query(NotificationSubscription).filter_by(
                discussion_id=self.discussion_id, user_id=self.id)
            if lock:
                query = query.with_for_update()
            my_subscriptions = query.all()
            my_subscriptions_classes = {s.__class__ for s in my_subscriptions}
            missing = set(needed_classes) - my_subscriptions_classes
            return my_subscriptions, missing
        my_subscriptions, missing = get_subcriptions(False)
        if not missing:
            return my_subscriptions, False
        my_subscriptions, missing = get_subcriptions(True)
        if not missing:
            return my_subscriptions, False
        # TODO: Fill from config.
        subscribed = defaultdict(bool)
        role_name = self.for_role.name.split(':')[-1]
        default_config = config.get_config().get(
            ".".join(("subscriptions", role_name, "default")),
            "FOLLOW_SYNTHESES")
        for role in default_config.split('\n'):
            subscribed[role.strip()] = True
        defaults = [
            cls(
                discussion_id=self.discussion_id,
                user_id=self.id,
                creation_origin=NotificationCreationOrigin.DISCUSSION_DEFAULT,
                status=(NotificationSubscriptionStatus.ACTIVE
                        if subscribed[cls.__mapper__.polymorphic_identity.name]
                        else NotificationSubscriptionStatus.INACTIVE_DFT))
            for cls in missing
        ]
        for d in defaults:
            self.db.add(d)
        self.db.flush()
        return chain(my_subscriptions, defaults), True


Index("user_template", "discussion_id", "role_id")


class PartnerOrganization(DiscussionBoundBase):
    """A corporate entity"""
    __tablename__ = "partner_organization"
    id = Column(Integer, primary_key=True,
        info={'rdf': QuadMapPatternS(None, ASSEMBL.db_id)})

    discussion_id = Column(Integer, ForeignKey(
        "discussion.id", ondelete='CASCADE'),
        info={'rdf': QuadMapPatternS(None, DCTERMS.contributor)})
    discussion = relationship(
        'Discussion', backref=backref(
            'partner_organizations', cascade="all, delete-orphan"),
        info={'rdf': QuadMapPatternS(None, ASSEMBL.in_conversation)})

    name = Column(CoerceUnicode(256),
        info={'rdf': QuadMapPatternS(None, FOAF.name)})

    description = Column(UnicodeText,
        info={'rdf': QuadMapPatternS(None, DCTERMS.description)})

    logo = Column(String(256),
        info={'rdf': QuadMapPatternS(None, FOAF.logo)})

    homepage = Column(String(256),
        info={'rdf': QuadMapPatternS(None, FOAF.homepage)})

    is_initiator = Column(Boolean)

    def unique_query(self, query):
        query = super(PartnerOrganization, self).unique_query(query)
        return query.filter_by(name=self.name), True

    def get_discussion_id(self):
        return self.discussion_id

    @classmethod
    def get_discussion_conditions(cls, discussion_id, alias_maker=None):
        return (cls.discussion_id == discussion_id,)

    crud_permissions = CrudPermissions(P_ADMIN_DISC)
