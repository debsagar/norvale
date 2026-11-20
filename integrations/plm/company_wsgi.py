"""Load Norvale's decision guard after InvenTree initializes Django."""
import os

from InvenTree.wsgi import application  # noqa: F401

if os.environ.get('NORVALE_PLM_CHANGE_GUARD') == '1':
    from InvenTree.norvale_change_guard import install

    install()

    upstream = application

    def application(environ, start_response):
        def guarded_response(status, headers, exc_info=None):
            return start_response(status, [*headers, ('X-Norvale-PLM-Change-Guard', '1')], exc_info)

        return upstream(environ, guarded_response)
