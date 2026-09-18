from fastapi import Depends

from bluweb_app.core.config import Settings, get_settings
from bluweb_app.services.security.url_security import URLSecurityService


def get_security_service(settings: Settings = Depends(get_settings)) -> URLSecurityService:
    return URLSecurityService(settings)
