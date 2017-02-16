from simplejson import dumps, loads
from string import Template

from pyramid.response import Response
from pyramid.view import view_config
from pyramid.security import (
    authenticated_userid, Everyone, NO_PERMISSION_REQUIRED)
from pyramid.httpexceptions import (
    HTTPNotFound, HTTPUnauthorized, HTTPBadRequest, HTTPClientError,
    HTTPOk, HTTPNoContent, HTTPForbidden, HTTPNotImplemented)

from assembl.auth import (
    P_ADMIN_DISC, P_SELF_REGISTER, P_SELF_REGISTER_REQUEST,
    R_PARTICIPANT, P_READ, CrudPermissions)
from assembl.models import (
    User, Discussion, LocalUserRole, AbstractAgentAccount, AgentProfile,
    UserLanguagePreference)
from assembl.auth.util import get_permissions
from ..traversal import (CollectionContext, InstanceContext, ClassContext)
from .. import JSONError
from . import (
    FORM_HEADER, JSON_HEADER, collection_view, instance_put_json,
    collection_add_json, instance_view, check_permissions, CreationResponse)
from assembl.lib.sqla import ObjectNotUniqueError


@view_config(
    context=ClassContext, request_method="PATCH",
    ctx_class=LocalUserRole, permission=NO_PERMISSION_REQUIRED)
@view_config(
    context=ClassContext, request_method="PUT",
    ctx_class=LocalUserRole, permission=NO_PERMISSION_REQUIRED)
@view_config(
    context=ClassContext, request_method="POST",
    ctx_class=LocalUserRole, permission=NO_PERMISSION_REQUIRED)
def add_local_role_on_class(request):
    # Did not securize this route, so forbid it.
    raise HTTPNotFound()


@view_config(
    context=CollectionContext, request_method="POST",
    ctx_named_collection="Discussion.local_user_roles",
    header=JSON_HEADER, renderer='json',
    permission=NO_PERMISSION_REQUIRED)
@view_config(
    context=CollectionContext, request_method="POST",
    ctx_named_collection="LocalRoleCollection.local_roles",
    header=JSON_HEADER, renderer='json',
    permission=NO_PERMISSION_REQUIRED)
def add_local_role(request):
    # Do not use check_permissions, this is a special case
    ctx = request.context
    user_id = authenticated_userid(request)
    if not user_id:
        raise HTTPUnauthorized()
    discussion_id = ctx.get_discussion_id()
    discussion = Discussion.get(discussion_id)
    user_uri = User.uri_generic(user_id)
    if discussion_id is None:
        raise HTTPBadRequest()
    permissions = get_permissions(user_id, discussion_id)
    json = request.json_body
    if "discussion" not in json:
        json["discussion"] = Discussion.uri_generic(discussion_id)
    requested_user = json.get('user', None)
    if not requested_user:
        json['user'] = requested_user = user_uri
    elif requested_user != user_uri and P_ADMIN_DISC not in permissions:
        raise HTTPUnauthorized()
    if P_ADMIN_DISC not in permissions:
        if P_SELF_REGISTER in permissions:
            json['requested'] = False
            json['role'] = R_PARTICIPANT
            req_user = User.get_instance(requested_user)
            if not discussion.check_authorized_email(req_user):
                raise HTTPForbidden()
        elif P_SELF_REGISTER_REQUEST in permissions:
            json['requested'] = True
        else:
            raise HTTPUnauthorized()
    try:
        instances = ctx.create_object("LocalUserRole", json, user_id)
    except HTTPClientError as e:
        raise e
    except Exception as e:
        raise HTTPBadRequest(e)
    if instances:
        first = instances[0]
        db = first.db
        for instance in instances:
            db.add(instance)
        db.flush()
        # Side effect: materialize subscriptions.
        if not first.requested:
            # relationship may not be initialized
            user = first.user or User.get(first.user_id)
            user.get_notification_subscriptions(discussion_id, True)

        # Update the user's AgentStatusInDiscussion
        user.update_agent_status_subscribe(discussion)

        view = request.GET.get('view', None) or 'default'
        permissions = get_permissions(
            user_id, ctx.get_discussion_id())
        return CreationResponse(first, user_id, permissions, view)


@view_config(
    context=InstanceContext, request_method="PATCH",
    ctx_named_collection_instance="Discussion.local_user_roles",
    permission=NO_PERMISSION_REQUIRED, header=JSON_HEADER, renderer='json')
@view_config(
    context=InstanceContext, request_method="PATCH",
    ctx_named_collection_instance="LocalRoleCollection.local_roles",
    permission=NO_PERMISSION_REQUIRED, header=JSON_HEADER, renderer='json')
@view_config(
    context=InstanceContext, request_method="PUT",
    ctx_named_collection_instance="Discussion.local_user_roles",
    permission=NO_PERMISSION_REQUIRED, header=JSON_HEADER, renderer='json')
@view_config(
    context=InstanceContext, request_method="PUT",
    ctx_named_collection_instance="LocalRoleCollection.local_roles",
    permission=NO_PERMISSION_REQUIRED, header=JSON_HEADER, renderer='json')
def set_local_role(request):
    # Do not use check_permissions, this is a special case
    ctx = request.context
    instance = ctx._instance
    user_id = authenticated_userid(request)
    if not user_id:
        raise HTTPUnauthorized()
    discussion_id = ctx.get_discussion_id()
    user_uri = User.uri_generic(user_id)
    if discussion_id is None:
        raise HTTPBadRequest()
    permissions = get_permissions(user_id, discussion_id)
    json = request.json_body
    requested_user = json.get('user', None)
    if not requested_user:
        json['user'] = requested_user = user_uri
    elif requested_user != user_uri and P_ADMIN_DISC not in permissions:
        raise HTTPUnauthorized()
    if P_ADMIN_DISC not in permissions:
        if P_SELF_REGISTER in permissions:
            json['requested'] = False
            json['role'] = R_PARTICIPANT
        elif P_SELF_REGISTER_REQUEST in permissions:
            json['requested'] = True
        else:
            raise HTTPUnauthorized()
    updated = instance.update_from_json(json, user_id, ctx)
    view = request.GET.get('view', None) or 'default'

    # Update the user's AgentStatusInDiscussion
    user = User.get(user_id)
    discussion = Discussion.get(discussion_id)
    user.update_agent_status_subscribe(discussion)

    if view == 'id_only':
        return [updated.uri()]
    else:
        return updated.generic_json(view, user_id, permissions)


@view_config(
    context=InstanceContext, request_method='DELETE',
    ctx_named_collection_instance="Discussion.local_user_roles",
    permission=NO_PERMISSION_REQUIRED, renderer='json')
@view_config(
    context=InstanceContext, request_method='DELETE',
    ctx_named_collection_instance="LocalRoleCollection.local_roles",
    permission=NO_PERMISSION_REQUIRED, renderer='json')
def delete_local_role(request):
    ctx = request.context
    instance = ctx._instance
    user_id = authenticated_userid(request)
    if not user_id:
        raise HTTPUnauthorized()
    discussion_id = ctx.get_discussion_id()

    if discussion_id is None:
        raise HTTPBadRequest()
    permissions = get_permissions(user_id, discussion_id)
    requested_user = instance.user
    if requested_user.id != user_id and P_ADMIN_DISC not in permissions:
        raise HTTPUnauthorized()

    user = User.get(user_id)
    discussion = Discussion.get(discussion_id)
    instance.db.delete(instance)
    # Update the user's AgentStatusInDiscussion
    user.update_agent_status_unsubscribe(discussion)
    instance.db.flush()  # maybe unnecessary
    return {}


@view_config(
    context=CollectionContext, request_method="POST",
    ctx_named_collection="Discussion.local_user_roles",
    header=FORM_HEADER, permission=NO_PERMISSION_REQUIRED)
@view_config(
    context=CollectionContext, request_method="POST",
    ctx_named_collection="LocalRoleCollection.local_roles",
    header=FORM_HEADER, permission=NO_PERMISSION_REQUIRED)
def use_json_header_for_LocalUserRole_POST(request):
    raise HTTPNotFound()


@view_config(
    context=CollectionContext, request_method="PUT",
    ctx_named_collection="Discussion.local_user_roles",
    header=FORM_HEADER, permission=NO_PERMISSION_REQUIRED)
@view_config(
    context=CollectionContext, request_method="PUT",
    ctx_named_collection="LocalRoleCollection.local_roles",
    header=FORM_HEADER, permission=NO_PERMISSION_REQUIRED)
def use_json_header_for_LocalUserRole_PUT(request):
    raise HTTPNotFound()


@view_config(context=CollectionContext, renderer='json', request_method='GET',
             ctx_collection_class=LocalUserRole,
             accept="application/json", permission=NO_PERMISSION_REQUIRED)
def view_localuserrole_collection(request):
    return collection_view(request, 'default')


@view_config(context=CollectionContext, renderer='json', request_method='GET',
             ctx_collection_class=AgentProfile,
             accept="application/json", permission=P_READ)
def view_profile_collection(request):
    ctx = request.context
    view = request.GET.get('view', None) or ctx.get_default_view() or 'default'
    content = collection_view(request)
    if view != "id_only":
        discussion = ctx.get_instance_of_class(Discussion)
        if discussion:
            from assembl.models import Post, AgentProfile
            num_posts_per_user = \
                AgentProfile.count_posts_in_discussion_all_profiles(discussion)
            for x in content:
                id = AgentProfile.get_database_id(x['@id'])
                if id in num_posts_per_user:
                    x['post_count'] = num_posts_per_user[id]
    return content


@view_config(context=InstanceContext, renderer='json', request_method='GET',
             ctx_instance_class=AgentProfile,
             accept="application/json", permission=P_READ)
def view_agent_profile(request):
    profile = instance_view(request)
    ctx = request.context
    view = ctx.get_default_view() or 'default'
    view = request.GET.get('view', view)
    if view not in ("id_only", "extended"):
        discussion = ctx.get_instance_of_class(Discussion)
        if discussion:
            profile['post_count'] = ctx._instance.count_posts_in_discussion(
                discussion.id)
    return profile


@view_config(
    context=InstanceContext, ctx_instance_class=AbstractAgentAccount,
    request_method='POST', name="verify", renderer='json',
    permission=NO_PERMISSION_REQUIRED)
def send_account_verification(request):
    ctx = request.context
    instance = ctx._instance
    if instance.verified:
        return HTTPNoContent(
            "No need to verify email <%s>" % (instance.email))
    from assembl.views.auth.views import send_confirmation_email
    request.matchdict = {}
    send_confirmation_email(request, instance)
    return {}


# TODO: Should I add a secure_connection condition?
@view_config(
    context=InstanceContext, ctx_instance_class=User,
    request_method='GET', name="verify_password", renderer='json',
    permission=NO_PERMISSION_REQUIRED)
def verify_password(request):
    ctx = request.context
    user = ctx._instance
    password = request.params.get('password', None)
    if password is not None:
        return user.check_password(password)
    raise HTTPBadRequest("Please provide a password")


@view_config(
    context=InstanceContext, ctx_instance_class=AbstractAgentAccount,
    request_method='DELETE', renderer='json', permission=NO_PERMISSION_REQUIRED)
def delete_abstract_agent_account(request):
    ctx = request.context
    user_id = authenticated_userid(request) or Everyone
    permissions = get_permissions(
        user_id, ctx.get_discussion_id())
    instance = ctx._instance
    if not instance.user_can(user_id, CrudPermissions.DELETE, permissions):
        return HTTPUnauthorized()
    if instance.email:
        accounts_with_mail = [a for a in instance.profile.accounts if a.email]
        if len(accounts_with_mail) == 1:
            raise JSONError(403, "This is the last account")
        if instance.verified:
            verified_accounts_with_mail = [
                a for a in accounts_with_mail if a.verified]
            if len(verified_accounts_with_mail) == 1:
                raise JSONError(403, "This is the last verified account")
    instance.db.delete(instance)
    return {}


# Should there not be a check that we're working on our own account????
@view_config(context=InstanceContext, request_method='PATCH',
             header=JSON_HEADER, ctx_instance_class=AbstractAgentAccount,
             renderer='json', permission=NO_PERMISSION_REQUIRED)
@view_config(context=InstanceContext, request_method='PUT', header=JSON_HEADER,
             ctx_instance_class=AbstractAgentAccount, renderer='json',
             permission=NO_PERMISSION_REQUIRED)
def put_abstract_agent_account(request):
    instance = request.context._instance
    old_preferred = instance.preferred
    new_preferred = request.json_body.get('preferred', False)
    if new_preferred and not instance.email:
        raise HTTPForbidden("Cannot prefer an account without email")
    if new_preferred and not instance.verified:
        raise HTTPForbidden("Cannot set a non-verified email as preferred")
    result = instance_put_json(request)
    assert instance.preferred == new_preferred
    if new_preferred and not old_preferred:
        for account in instance.profile.accounts:
            if account != instance:
                account.preferred = False
    return result


# Should there not be a check that we're working on our own account????
@view_config(context=CollectionContext, request_method='POST',
             header=JSON_HEADER, ctx_collection_class=AbstractAgentAccount,
             permission=NO_PERMISSION_REQUIRED)
def post_email_account(request):
    from assembl.views.auth.views import send_confirmation_email
    response = collection_add_json(request)
    request.matchdict = {}
    instance = request.context.collection_class.get_instance(response.location)
    send_confirmation_email(request, instance)
    return response


@view_config(
    context=InstanceContext, request_method='GET',
    ctx_instance_class=AgentProfile, permission=P_READ,
    renderer='json', name='interesting_ideas')
def interesting_ideas(request):
    from .discussion import get_analytics_alerts
    ctx = request.context
    target = request.context._instance
    user_id = authenticated_userid(request) or Everyone
    discussion_id = ctx.get_discussion_id()
    permissions = get_permissions(
        user_id, discussion_id)
    if P_READ not in permissions:
        raise HTTPUnauthorized()
    if user_id != target.id and P_ADMIN_DISC not in permissions:
        raise HTTPUnauthorized()
    discussion = Discussion.get(discussion_id)
    if not discussion:
        raise HTTPNotFound()
    result = get_analytics_alerts(
        discussion, target.id,
        ["interesting_to_me"], False)
    result = loads(result)['responses'][0]['data'][0]['suggestions']
    result = {x['targetID']: x['arguments']['score'] for x in result}
    return result


@view_config(context=CollectionContext, request_method='POST', renderer="json",
             header=JSON_HEADER, ctx_collection_class=UserLanguagePreference,
             permission=NO_PERMISSION_REQUIRED)
def add_user_language_preference(request):
    ctx = request.context
    user_id = authenticated_userid(request) or Everyone
    permissions = get_permissions(
        user_id, ctx.get_discussion_id())
    check_permissions(ctx, user_id, permissions, CrudPermissions.CREATE)
    typename = ctx.collection_class.external_typename()
    json = request.json_body
    try:
        instances = ctx.create_object(typename, json, user_id)
    except ObjectNotUniqueError as e:
        raise JSONError(409, str(e))
    except Exception as e:
        raise HTTPBadRequest(e)
    if instances:
        first = instances[0]
        db = first.db
        for instance in instances:
            db.add(instance)
        db.flush()
        view = request.GET.get('view', None) or 'default'
        return CreationResponse(first, user_id, permissions, view)


@view_config(context=InstanceContext, request_method='PUT', renderer="json",
             header=JSON_HEADER, ctx_instance_class=UserLanguagePreference,
             permission=NO_PERMISSION_REQUIRED)
@view_config(context=InstanceContext, request_method='PATCH', renderer="json",
             header=JSON_HEADER, ctx_instance_class=UserLanguagePreference,
             permission=NO_PERMISSION_REQUIRED)
def modify_user_language_preference(request):
    json_data = request.json_body
    ctx = request.context
    user_id = authenticated_userid(request) or Everyone
    permissions = get_permissions(
        user_id, ctx.get_discussion_id())
    instance = ctx._instance
    if not instance.user_can(user_id, CrudPermissions.UPDATE, permissions):
        return HTTPUnauthorized()
    try:
        updated = instance.update_from_json(json_data, user_id, ctx)
        view = request.GET.get('view', None) or 'default'
        if view == 'id_only':
            return [updated.uri()]
        else:
            return updated.generic_json(view, user_id, permissions)

    except NotImplemented:
        raise HTTPNotImplemented()
    except ObjectNotUniqueError as e:
        raise JSONError(409, str(e))
