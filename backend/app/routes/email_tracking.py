"""
Email open tracking pixel.

  - /api/t/o/<token>.gif   public, no auth: called by the recipient's mail
                           client when it loads the tracking image.

Always answers with the same 1x1 GIF, whether or not the token is known, so
the response reveals nothing about which tokens exist.

See `backend/app/services/email_tracking.py` for the tracking logic.
"""
import logging
from flask import Blueprint, Response

from app.services.email_tracking import PIXEL_GIF, record_open

logger = logging.getLogger(__name__)

email_tracking_bp = Blueprint('email_tracking', __name__, url_prefix='/api/t')


@email_tracking_bp.route('/o/<token>.gif', methods=['GET'])
def open_pixel(token):
    try:
        record_open(token)
    except Exception as e:
        logger.warning("[email_tracking] pixel handler error: %s", e)
    resp = Response(PIXEL_GIF, mimetype='image/gif')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, private, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp
