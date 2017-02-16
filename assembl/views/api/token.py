"""API to get an authentication token, for use by Web annotation"""
import json
import transaction

from pyramid.security import authenticated_userid, Everyone, NO_PERMISSION_REQUIRED
from pyramid.httpexceptions import HTTPUnauthorized
from pyramid.view import view_config
from pyramid.response import Response

from assembl.lib.web_token import encode_token

_ac = 'Access-Control-'
option_headers = [
    (_ac + 'Allow-Headers', 'X-Requested-With, Content-Type, Content-Length'),
    (_ac + 'Allow-Methods', 'GET, OPTIONS'),
    (_ac + 'Max-Age', '86400')
]


@view_config(route_name='csrf_token', request_method='OPTIONS',
             permission=NO_PERMISSION_REQUIRED)
def auth_token_options(request):
    return auth_token(request, option_headers)


@view_config(route_name='csrf_token', request_method='GET',
             permission=NO_PERMISSION_REQUIRED)
def auth_token(request, extra_headers=None):
    headers = []
    if 'origin' in request.headers:
        headers.extend([
            (_ac + 'Allow-Origin', request.headers['origin']),
            (_ac + 'Allow-Credentials', 'true'),
            (_ac + 'Expose-Headers', 'Location, Content-Type, Content-Length'),
        ])
        if extra_headers:
            headers.extend(extra_headers)
    user_id = authenticated_userid(request)
    payload = {
        'consumerKey': 'assembl', 'userId': (user_id or Everyone), 'ttl': 86400
    }
    token = encode_token(payload, request.registry.settings['session.secret'])
    return Response(token, 200, headers, content_type='text/plain')
