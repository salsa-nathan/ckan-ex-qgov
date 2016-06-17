"""Provides a self-contained filter to prevent Cross-Site Request Forgery,
based on the Double Submit Cookie pattern,
www.owasp.org/index.php/Cross-Site_Request_Forgery_(CSRF)_Prevention_Cheat_Sheet#Double_Submit_Cookie

The filter can be enabled simply by invoking 'intercept_csrf()'.
"""
import ckan.lib.base as base
import re
from re import DOTALL, IGNORECASE, MULTILINE
from genshi.template import MarkupTemplate
from logging import getLogger
from pylons import request, response

LOG = getLogger(__name__)

RAW_RENDER = base.render
RAW_RENDER_JINJA = base.render_jinja2
RAW_BEFORE = base.BaseController.__before__

""" Used as the cookie name and input field name.
"""
TOKEN_FIELD_NAME = 'token'

# We need to edit confirm-action links, which get intercepted by JavaScript,
#regardless of which order their 'data-module' and 'href' attributes appear.
CONFIRM_LINK = re.compile(r'(<a [^>]*data-module=["\']confirm-action["\'][^>]*href=["\'][^"\'?]+)(["\'])', IGNORECASE | MULTILINE)
CONFIRM_LINK_REVERSED = re.compile(r'(<a [^>]*href=["\'][^"\'?]+)(["\'][^>]*data-module=["\']confirm-action["\'])', IGNORECASE | MULTILINE)

"""
This will match a POST form that has whitespace after the opening tag (which all existing forms do).
Once we have injected a token immediately after the opening tag,
it won't match any more, which avoids redundant injection.
"""
POST_FORM = re.compile(r'(<form [^>]*method=["\']post["\'][^>]*>)([^<]*\s<)', IGNORECASE | MULTILINE)

"""The format of the token HTML field.
"""
TOKEN_PATTERN = r'<input type="hidden" name="' + TOKEN_FIELD_NAME + '" value="{token}"/>'
TOKEN_SEARCH_PATTERN = re.compile(TOKEN_PATTERN.format(token=r'([0-9a-f]+)'))
API_URL = re.compile(r'^/api\b.*')

def is_logged_in():
    return request.cookies.get("auth_tkt")

""" Rewrite HTML to insert tokens if applicable.
"""
def anti_csrf_render(template_name, extra_vars=None, cache_key=None, cache_type=None, cache_expire=None, method='xhtml', loader_class=MarkupTemplate, cache_force=None, renderer=None):
    html = apply_token(RAW_RENDER(template_name, extra_vars, cache_key, cache_type, cache_expire, method, loader_class, cache_force, renderer))
    return html

def anti_csrf_render_jinja2(template_name, extra_vars=None):
    html = apply_token(RAW_RENDER_JINJA(template_name, extra_vars))
    return html

def apply_token(html):
    if not is_logged_in() or not POST_FORM.search(html):
        return html

    token_match = TOKEN_SEARCH_PATTERN.search(html)
    if token_match:
        token = token_match.group(1)
    else:
        token = get_server_token()

    def insert_form_token(form_match):
        return form_match.group(1) + TOKEN_PATTERN.format(token=token) + form_match.group(2)

    def insert_link_token(link_match):
        return link_match.group(1) + '?' + TOKEN_FIELD_NAME + '=' + token + link_match.group(2)

    return CONFIRM_LINK_REVERSED.sub(insert_link_token, CONFIRM_LINK.sub(insert_link_token, POST_FORM.sub(insert_form_token, html)))

def get_server_token():
    """Retrieve the token expected by the server.

    This will be retrieved from the 'token' cookie, if it exists.
    If not, a new token will be generated and a new cookie set.

    This can be used either to populate a new form on a GET request, or to validate a POST request
    (since a missing cookie would result in a new token, which would certainly not match the submitted form).
    """
    # ensure that the same token is used when a page is assembled from pieces
    if request.environ['webob.adhoc_attrs'].has_key('server_token'):
        token = request.server_token
    elif request.cookies.has_key(TOKEN_FIELD_NAME):
        token = request.cookies.pop(TOKEN_FIELD_NAME)
    else:
        import binascii, os
        token = binascii.hexlify(os.urandom(32))
        response.set_cookie(TOKEN_FIELD_NAME, token, max_age=600, httponly=True)

    if token is None or token.strip() == "":
        csrf_fail("Server token is blank")

    request.server_token = token
    return token

# Check token on applicable requests

def is_request_exempt():
    return not is_logged_in() or API_URL.match(request.path) or request.method in {'GET', 'HEAD', 'OPTIONS'}

def anti_csrf_before(obj, action, **params):
    if not is_request_exempt() and get_server_token() != get_post_token():
        csrf_fail("Could not match session token with form token")

    RAW_BEFORE(obj, action)

def csrf_fail(message):
    from pylons.controllers.util import abort
    LOG.error(message)
    abort(403, "Your form submission could not be validated")

def get_post_token():
    """Retrieve the token provided by the client.

    This is normally a single 'token' parameter in the POST body.
    However, for compatibility with 'confirm-action' links,
    it is also acceptable to provide the token as a query string parameter,
    if it is the *only* parameter of either kind.
    """
    if request.environ['webob.adhoc_attrs'].has_key(TOKEN_FIELD_NAME):
        return request.token

    # handle query string token if there are no other parameters
    # this is needed for the 'confirm-action' JavaScript module
    if not request.POST and len(request.GET) == 1 and len(request.GET.getall(TOKEN_FIELD_NAME)) == 1:
        request.token = request.GET.getone(TOKEN_FIELD_NAME)
        del request.GET[TOKEN_FIELD_NAME]
        return request.token

    postTokens = request.POST.getall(TOKEN_FIELD_NAME)
    if not postTokens:
        csrf_fail("Missing CSRF token in form submission")
    elif len(postTokens) > 1:
        csrf_fail("More than one CSRF token in form submission")
    else:
        request.token = postTokens[0]

    # drop token from request so it doesn't populate resource extras
    del request.POST[TOKEN_FIELD_NAME]

    return request.token

def intercept_csrf():
    base.render = anti_csrf_render
    base.render_jinja2 = anti_csrf_render_jinja2
    base.BaseController.__before__ = anti_csrf_before
