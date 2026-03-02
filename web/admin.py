"""
Admin-панель и FastAPI приложение — web/admin.py.

Предоставляет:
    - SQLAdmin CRUD-интерфейс для всех таблиц БД
    - HTTP Basic Auth (secrets.compare_digest против timing attacks)
    - /dashboard — Chart.js дашборд со статистикой
    - /api/dashboard/* — JSON endpoints для Chart.js

Доступ: http://localhost:8000/admin  (ADMIN_USERNAME / ADMIN_PASSWORD из .env)
"""

import secrets
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request as StarletteRequest

from core.config import settings
from db.database import engine
from db.models import (
    AgentLog,
    ArticleEmbedding,
    PipelineRun,
    PostStats,
    PublishedPost,
    RawArticle,
    ScheduleSlot,
    Setting,
    Source,
)

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="NewsBot Control Panel",
    docs_url=None,    # Отключаем Swagger UI в продакшне
    redoc_url=None,
)

templates = Jinja2Templates(directory="web/templates")

# ── Basic Auth ────────────────────────────────────────────────────────────────

security = HTTPBasic()


def verify_credentials(
    credentials: HTTPBasicCredentials = Depends(security),
) -> str:
    """
    Проверяет HTTP Basic Auth credentials.
    secrets.compare_digest защищает от timing-атак.

    Returns:
        username при успехе.

    Raises:
        HTTPException 401 при неверных credentials.
    """
    ok_user = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        settings.ADMIN_USERNAME.encode("utf-8"),
    )
    ok_pass = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        settings.ADMIN_PASSWORD.encode("utf-8"),
    )
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Неверные credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ── SQLAdmin Authentication Backend ──────────────────────────────────────────

class BasicAuthBackend(AuthenticationBackend):
    """Адаптер Basic Auth для SQLAdmin."""

    async def login(self, request: StarletteRequest) -> bool:
        form = await request.form()
        username = form.get("username", "")
        password = form.get("password", "")
        ok_user = secrets.compare_digest(
            str(username).encode(), settings.ADMIN_USERNAME.encode()
        )
        ok_pass = secrets.compare_digest(
            str(password).encode(), settings.ADMIN_PASSWORD.encode()
        )
        if ok_user and ok_pass:
            request.session["authenticated"] = True
            return True
        return False

    async def logout(self, request: StarletteRequest) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: StarletteRequest) -> Optional[bool]:
        return request.session.get("authenticated", False)


# ── SQLAdmin Views ────────────────────────────────────────────────────────────

class SourceAdmin(ModelView, model=Source):
    name         = "Источник"
    name_plural  = "RSS Источники"
    icon         = "fa-solid fa-rss"

    column_list          = [Source.id, Source.name, Source.category,
                             Source.is_active, Source.fetch_count, Source.last_fetched_at]
    column_searchable_list = [Source.name, Source.url]
    column_sortable_list   = [Source.fetch_count, Source.last_fetched_at]

    can_create = True
    can_edit   = True
    can_delete = True


class SettingAdmin(ModelView, model=Setting):
    name         = "Настройка"
    name_plural  = "Настройки"
    icon         = "fa-solid fa-gear"

    column_list = [Setting.key, Setting.value, Setting.description, Setting.updated_at]

    can_create = False   # Настройки создаются только через init_db.py
    can_delete = False


class ScheduleSlotAdmin(ModelView, model=ScheduleSlot):
    name         = "Слот расписания"
    name_plural  = "Расписание"
    icon         = "fa-solid fa-clock"

    column_list = [ScheduleSlot.id, ScheduleSlot.hour,
                   ScheduleSlot.minute, ScheduleSlot.days_of_week, ScheduleSlot.is_active]

    can_create = True
    can_edit   = True
    can_delete = True

    async def after_model_change(self, data, model, is_created, request):
        """После изменения расписания — перезагружаем планировщик."""
        from core.scheduler import reload_schedule
        await reload_schedule()


class RawArticleAdmin(ModelView, model=RawArticle):
    name         = "Статья"
    name_plural  = "Сырые статьи"
    icon         = "fa-solid fa-newspaper"

    column_list          = [RawArticle.id, RawArticle.title, RawArticle.status,
                             RawArticle.fetched_at, RawArticle.retry_count]
    column_searchable_list = [RawArticle.title, RawArticle.url]
    column_sortable_list   = [RawArticle.fetched_at, RawArticle.status]

    can_create = False
    can_edit   = False
    can_delete = False

    page_size = 50


class PipelineRunAdmin(ModelView, model=PipelineRun):
    name         = "Прогон"
    name_plural  = "Прогоны пайплайна"
    icon         = "fa-solid fa-play"

    column_list = [PipelineRun.id, PipelineRun.started_at, PipelineRun.finished_at,
                   PipelineRun.status, PipelineRun.articles_found,
                   PipelineRun.articles_verified, PipelineRun.articles_published]
    column_sortable_list = [PipelineRun.started_at, PipelineRun.status]

    can_create = False
    can_edit   = False

    page_size = 30


class AgentLogAdmin(ModelView, model=AgentLog):
    name         = "Лог агента"
    name_plural  = "Логи агентов"
    icon         = "fa-solid fa-list"

    column_list = [AgentLog.id, AgentLog.created_at, AgentLog.agent_name,
                   AgentLog.status, AgentLog.reason, AgentLog.latency_ms,
                   AgentLog.input_tokens, AgentLog.output_tokens]
    column_searchable_list = [AgentLog.agent_name, AgentLog.status]
    column_sortable_list   = [AgentLog.created_at, AgentLog.latency_ms]

    can_create = False
    can_edit   = False
    can_delete = False

    page_size = 50


class PublishedPostAdmin(ModelView, model=PublishedPost):
    name         = "Пост"
    name_plural  = "Опубликованные посты"
    icon         = "fa-brands fa-telegram"

    column_list = [PublishedPost.id, PublishedPost.published_at,
                   PublishedPost.source_name, PublishedPost.telegram_msg_id,
                   PublishedPost.has_image]
    column_sortable_list = [PublishedPost.published_at]

    can_create = False
    can_edit   = False

    page_size = 20


# ── Инициализация SQLAdmin ────────────────────────────────────────────────────

from starlette.middleware.sessions import SessionMiddleware  # noqa: E402

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.ADMIN_PASSWORD or "newsbot-session-secret",
    https_only=False,
)

admin = Admin(
    app,
    engine,
    title="NewsBot Admin",
    authentication_backend=BasicAuthBackend(secret_key=settings.ADMIN_PASSWORD),
)

admin.add_view(SourceAdmin)
admin.add_view(SettingAdmin)
admin.add_view(ScheduleSlotAdmin)
admin.add_view(RawArticleAdmin)
admin.add_view(PipelineRunAdmin)
admin.add_view(AgentLogAdmin)
admin.add_view(PublishedPostAdmin)

# ── Ссылка на Dashboard в меню sqladmin ──────────────────────────────────────

from sqladmin import BaseView, expose as sqladmin_expose  # noqa: E402
from starlette.responses import RedirectResponse as _Redirect  # noqa: E402


class DashboardLink(BaseView):
    """Кнопка перехода на Dashboard в боковом меню sqladmin."""
    name     = "← Dashboard"
    icon     = "fa-solid fa-gauge-high"
    identity = "back-to-dashboard"

    @sqladmin_expose("/goto-dashboard", methods=["GET"])
    async def goto_dashboard(self, request: Request) -> _Redirect:
        return _Redirect(url="/dashboard")


admin.add_base_view(DashboardLink)


# ── Dashboard routes ──────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, _=Depends(verify_credentials)):
    """Главная страница дашборда с Chart.js графиками."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/health")
async def health():
    """Health-check endpoint для Docker и мониторинга."""
    return {"status": "ok"}


# Регистрируем роуты дашборда: /api/dashboard/* и /api/pipeline/run
# Импорт должен быть ПОСЛЕ определения app чтобы избежать circular import
import web.dashboard  # noqa: F401, E402
