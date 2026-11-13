"""Cookie-only locale selection: LANGUAGE_CODE default, explicit opt-in to others."""
from django.conf import settings
from django.middleware.locale import LocaleMiddleware
from django.utils import translation


class CookieLocaleMiddleware(LocaleMiddleware):
    """LocaleMiddleware that ignores the Accept-Language header entirely.

    Language = a valid ``django_language`` cookie (set via the built-in
    ``set_language`` view behind the language switcher), else
    ``settings.LANGUAGE_CODE``. Browser preferences never influence the
    choice - every visitor starts in the configured default language.
    """

    def process_request(self, request):
        lang = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
        if not (lang and translation.check_for_language(lang)):
            lang = settings.LANGUAGE_CODE
        translation.activate(lang)
        request.LANGUAGE_CODE = translation.get_language()
