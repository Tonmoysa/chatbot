"""
Django settings for HR AI chatbot microservice.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "django-insecure-dev-only")
DEBUG = os.environ.get("DEBUG", "true").lower() in ("1", "true", "yes")

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "chat",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "chat.middleware.TraceIdMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- HR microservice configuration ---
HR_SERVICE_API_KEY = os.environ.get("HR_SERVICE_API_KEY", "")
USE_MOCK_CRM = os.environ.get("USE_MOCK", "true").lower() in ("1", "true", "yes")
PHP_CRM_BASE_URL = os.environ.get("PHP_CRM_BASE_URL", "").rstrip("/")
PHP_CRM_API_KEY = os.environ.get("PHP_CRM_API_KEY", "")

CRM_HTTP_TIMEOUT_SECONDS = float(os.environ.get("CRM_HTTP_TIMEOUT_SECONDS", "5"))
CRM_HTTP_MAX_RETRIES = int(os.environ.get("CRM_HTTP_MAX_RETRIES", "2"))

LLM_API_BASE_URL = os.environ.get("LLM_API_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "25"))

REDIS_URL = os.environ.get("REDIS_URL", "").strip()

if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "hr-chatbot-local",
        }
    }

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["chat.authentication.ApiKeyAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "user": "180/minute",
    },
    "EXCEPTION_HANDLER": "chat.exceptions.hr_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

ENABLE_API_DOCS = os.environ.get("ENABLE_API_DOCS", "true").lower() in (
    "1",
    "true",
    "yes",
)

SPECTACULAR_SETTINGS = {
    "TITLE": "HR AI Chatbot API",
    "DESCRIPTION": "HR chatbot microservice. Send `X-API-Key` on protected routes. "
    "Chat responses include `X-Session-Id` for multi-turn `session_id`.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            "ApiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": "Same value as server env `HR_SERVICE_API_KEY`.",
            }
        }
    },
    "SECURITY": [{"ApiKeyAuth": []}],
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "jsonish": {
            "format": "%(levelname)s %(name)s trace_id=%(trace_id)s %(message)s",
        },
        "simple": {"format": "%(levelname)s %(name)s %(message)s"},
    },
    "filters": {
        "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
    },
    "loggers": {
        "hr_chatbot": {"handlers": ["console"], "level": "INFO"},
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
