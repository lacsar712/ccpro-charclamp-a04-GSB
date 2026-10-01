from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from litestar import Controller, MediaType, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import selectinload

from charclamp.domain.models import BurnShift, Ceasefire, Clamp, Site, User, utcnow
from charclamp.domain.rules import (
    RuleError,
    assert_can_register_shift,
    assert_can_set_clamp_status,
    can_mark_clamp_drawn,
)
from charclamp.infra.db import SessionLocal
from charclamp.infra.security import verify_password

STATUS_LABELS = {
    Clamp.STATUS_STACKED: "已码窑",
    Clamp.STATUS_BURNING: "焖烧中",
    Clamp.STATUS_DRAWN: "已出炭",
}


def _set_flash(request: Request, message: str, category: str = "ok") -> None:
    data = dict(request.session or {})
    data["flash"] = message
    data["flash_cat"] = category
    request.set_session(data)


def _pop_flash(request: Request) -> tuple[str | None, str | None]:
    data = dict(request.session or {})
    message = data.pop("flash", None)
    category = data.pop("flash_cat", None)
    if message is not None or category is not None:
        request.set_session(data)
    return message, category


def _parse_optional_int(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _as_aware_utc(value: datetime) -> datetime:
    """datetime-local 提交为朴素本地时间，统一按 UTC 落库。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _open_ceasefires_by_site(db) -> dict[int, Ceasefire]:
    """当前所有未收停火令，按窑场 id 索引（同窑场至多一条）。"""
    rows = (
        await db.execute(
            select(Ceasefire)
            .where(Ceasefire.lifted_at.is_(None))
            .options(selectinload(Ceasefire.site), selectinload(Ceasefire.issuer))
        )
    ).scalars().all()
    return {order.site_id: order for order in rows}


async def _load_timeline_context(clamp_id: int | None = None) -> dict[str, Any]:
    async with SessionLocal() as db:
        clamps = list(
            (
                await db.execute(
                    select(Clamp)
                    .options(selectinload(Clamp.site), selectinload(Clamp.shifts))
                    .order_by(Clamp.code)
                )
            )
            .scalars()
            .all()
        )
        query = (
            select(BurnShift)
            .options(selectinload(BurnShift.clamp).selectinload(Clamp.site))
            .order_by(BurnShift.started_at.desc())
        )
        if clamp_id is not None:
            query = query.where(BurnShift.clamp_id == clamp_id)
        shifts = list((await db.execute(query)).scalars().all())
        open_by_site = await _open_ceasefires_by_site(db)
        site_name = clamps[0].site.name if clamps else "乌石岗焖烧坞"
    return {
        "clamps": clamps,
        "shifts": shifts,
        "active_clamp_id": clamp_id,
        "status_labels": STATUS_LABELS,
        "site_name": site_name,
        "open_by_site": open_by_site,
    }


class AuthController(Controller):
    path = ""
    tags = ["auth"]

    @get("/login", media_type=MediaType.HTML)
    async def login_page(self, request: Request) -> Template:
        flash, flash_cat = _pop_flash(request)
        return Template(
            template_name="login.html",
            context={"flash": flash, "flash_cat": flash_cat},
        )

    @post("/login")
    async def login(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        async with SessionLocal() as db:
            result = await db.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if not user or not verify_password(password, user.password_hash):
                request.set_session({"flash": "用户名或密码错误", "flash_cat": "error"})
                return Redirect("/login")
            request.set_session({"user_id": user.id})
        return Redirect("/")

    @get("/logout")
    async def logout(self, request: Request) -> Redirect:
        request.clear_session()
        return Redirect("/login")


class TimelineController(Controller):
    path = ""
    tags = ["timeline"]

    @get("/", media_type=MediaType.HTML)
    async def timeline(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        flash, flash_cat = _pop_flash(request)
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        ctx = await _load_timeline_context(clamp_id)
        return Template(
            template_name="timeline.html",
            context={
                **ctx,
                "user": request.user,
                "flash": flash,
                "flash_cat": flash_cat,
            },
        )

    @get("/timeline/partial", media_type=MediaType.HTML)
    async def timeline_partial(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        ctx = await _load_timeline_context(clamp_id)
        return Template(
            template_name="partials/board.html",
            context={
                **ctx,
                "user": request.user,
            },
        )

    @get("/drawer/shift-new", media_type=MediaType.HTML)
    async def drawer_shift_new(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        clamp_id = _parse_optional_int(request.query_params.get("clamp_id"))
        async with SessionLocal() as db:
            clamps = list(
                (
                    await db.execute(
                        select(Clamp).options(selectinload(Clamp.site)).order_by(Clamp.code)
                    )
                )
                .scalars()
                .all()
            )
            open_by_site = await _open_ceasefires_by_site(db)
        return Template(
            template_name="partials/drawer_shift.html",
            context={
                "clamps": clamps,
                "preselect_clamp_id": clamp_id,
                "open_by_site": open_by_site,
                "user": request.user,
            },
        )

    @get("/drawer/clamp/{clamp_id:int}", media_type=MediaType.HTML)
    async def drawer_clamp(self, request: Request, clamp_id: int) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        async with SessionLocal() as db:
            result = await db.execute(
                select(Clamp)
                .where(Clamp.id == clamp_id)
                .options(selectinload(Clamp.shifts), selectinload(Clamp.site))
            )
            clamp = result.scalar_one_or_none()
            if not clamp:
                return Redirect("/")
            open_by_site = await _open_ceasefires_by_site(db)
        can_drawn, drawn_msg = can_mark_clamp_drawn(clamp)
        return Template(
            template_name="partials/drawer_clamp.html",
            context={
                "clamp": clamp,
                "status_labels": STATUS_LABELS,
                "can_drawn": can_drawn,
                "drawn_msg": drawn_msg,
                "ceasefire": open_by_site.get(clamp.site_id),
                "user": request.user,
            },
        )


class ShiftController(Controller):
    path = "/shifts"
    tags = ["shifts"]

    @post("/new")
    async def create_shift(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        started_raw = data.get("started_at") or ""
        started_at = (
            _as_aware_utc(datetime.fromisoformat(started_raw))
            if started_raw
            else utcnow()
        )
        peak_raw = (data.get("peak_temp_c") or "").strip()
        peak = float(peak_raw) if peak_raw else None
        try:
            clamp_id = int(data["clamp_id"])
        except (KeyError, TypeError, ValueError):
            _set_flash(request, "缺少有效的炭窑，未登记班次", "error")
            return Redirect("/")
        async with SessionLocal() as db:
            clamp = (
                await db.execute(
                    select(Clamp)
                    .where(Clamp.id == clamp_id)
                    .options(selectinload(Clamp.site))
                )
            ).scalar_one_or_none()
            if not clamp:
                _set_flash(request, "炭窑不存在，未登记班次", "error")
                return Redirect("/")
            # 雨棚停火令：后端强制拦截，按钮藏了也登记不了。
            open_order = (
                await db.execute(
                    select(Ceasefire).where(
                        Ceasefire.site_id == clamp.site_id,
                        Ceasefire.lifted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            try:
                assert_can_register_shift(clamp, open_order)
            except RuleError as exc:
                _set_flash(request, str(exc), "error")
                return Redirect(f"/?clamp_id={clamp_id}")
            shift = BurnShift(
                clamp_id=clamp_id,
                started_at=started_at,
                peak_temp_c=peak,
                charcoal_grade=(data.get("charcoal_grade") or "B").strip(),
                notes=(data.get("notes") or "").strip(),
            )
            db.add(shift)
            if clamp.status == Clamp.STATUS_STACKED:
                clamp.status = Clamp.STATUS_BURNING
            await db.commit()
        _set_flash(request, "焖烧班次已登记", "ok")
        return Redirect(f"/?clamp_id={clamp_id}")


class ClampController(Controller):
    path = "/clamps"
    tags = ["clamps"]

    @post("/{clamp_id:int}/status")
    async def set_status(
        self,
        request: Request,
        clamp_id: int,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        new_status = (data.get("status") or "").strip()
        async with SessionLocal() as db:
            result = await db.execute(
                select(Clamp)
                .where(Clamp.id == clamp_id)
                .options(selectinload(Clamp.shifts))
            )
            clamp = result.scalar_one_or_none()
            if not clamp:
                return Redirect("/")
            # 出炭与改回已码窑不走停火令校验，仍只受原峰值规则约束。
            try:
                assert_can_set_clamp_status(clamp, new_status)
                clamp.status = new_status
                await db.commit()
                _set_flash(request, f"窑 {clamp.code} 状态已更新", "ok")
            except RuleError as exc:
                _set_flash(request, str(exc), "error")
        return Redirect(f"/?clamp_id={clamp_id}")


class CeasefireController(Controller):
    """雨棚停火令：挂令 / 收令 / 现行与历史列表，仅管理员可写。"""

    path = ""
    tags = ["ceasefires"]

    @get("/ceasefires", media_type=MediaType.HTML)
    async def list_page(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        flash, flash_cat = _pop_flash(request)
        async with SessionLocal() as db:
            orders = list(
                (
                    await db.execute(
                        select(Ceasefire)
                        .options(
                            selectinload(Ceasefire.site),
                            selectinload(Ceasefire.issuer),
                            selectinload(Ceasefire.lifter),
                        )
                        .order_by(
                            Ceasefire.lifted_at.is_(None).desc(),
                            Ceasefire.effective_at.desc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            sites = list((await db.execute(select(Site).order_by(Site.name))).scalars().all())
            open_by_site = await _open_ceasefires_by_site(db)
        return Template(
            template_name="ceasefires.html",
            context={
                "orders": orders,
                "sites": sites,
                "open_site_ids": set(open_by_site),
                "user": request.user,
                "is_admin": getattr(request.user, "role", None) == "admin",
                "flash": flash,
                "flash_cat": flash_cat,
            },
        )

    @post("/ceasefires/issue")
    async def issue(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        # 后端角色校验：非管理员直接中文回绝，不依赖前端藏表单。
        if request.user.role != "admin":
            _set_flash(request, "仅管理员可挂雨棚停火令", "error")
            return Redirect("/ceasefires")
        try:
            site_id = int(data.get("site_id"))
        except (TypeError, ValueError):
            _set_flash(request, "请选择窑场", "error")
            return Redirect("/ceasefires")
        rain_summary = (data.get("rain_summary") or "").strip()
        if not rain_summary:
            _set_flash(request, "请填写降雨摘要", "error")
            return Redirect("/ceasefires")
        effective_raw = (data.get("effective_at") or "").strip()
        if effective_raw:
            try:
                effective_at = _as_aware_utc(datetime.fromisoformat(effective_raw))
            except ValueError:
                _set_flash(request, "生效时刻格式不正确", "error")
                return Redirect("/ceasefires")
        else:
            effective_at = utcnow()
        planned_raw = (data.get("planned_lift_date") or "").strip()
        planned_date: date | None = None
        if planned_raw:
            try:
                planned_date = date.fromisoformat(planned_raw)
            except ValueError:
                _set_flash(request, "计划收令日格式不正确", "error")
                return Redirect("/ceasefires")

        async with SessionLocal() as db:
            site_stmt = select(Site).where(Site.id == site_id)
            # 同行串行化两名管理员的并发挂令（SQLite 不支持行锁，由唯一索引兜底）。
            if db.bind.dialect.name == "postgresql":
                site_stmt = site_stmt.with_for_update()
            site = (await db.execute(site_stmt)).scalar_one_or_none()
            if not site:
                _set_flash(request, "窑场不存在，未挂令", "error")
                return Redirect("/ceasefires")
            # 名称先缓存：commit/rollback 后 ORM 对象可能过期，
            # 异步上下文里再访问关系属性会触发懒加载报错。
            site_name = site.name
            existing = (
                await db.execute(
                    select(Ceasefire).where(
                        Ceasefire.site_id == site_id,
                        Ceasefire.lifted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                _set_flash(
                    request,
                    f"窑场「{site_name}」已有未收停火令（{existing.effective_at:%Y-%m-%d %H:%M} 挂出），不可重复挂令",
                    "error",
                )
                return Redirect("/ceasefires")
            order = Ceasefire(
                site_id=site_id,
                effective_at=effective_at,
                planned_lift_date=planned_date,
                rain_summary=rain_summary,
                issued_by=request.user.id,
            )
            db.add(order)
            try:
                await db.commit()
            except (IntegrityError, OperationalError):
                # 并发撞单：唯一部分索引保证同一窑场只有一条未收令
                # （SQLite 等锁竞争的库可能抛 OperationalError，回滚后复查即可）。
                await db.rollback()
                dup = (
                    await db.execute(
                        select(Ceasefire).where(
                            Ceasefire.site_id == site_id,
                            Ceasefire.lifted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if dup is not None:
                    _set_flash(
                        request,
                        f"窑场「{site_name}」已存在未收停火令，本次挂令未生效",
                        "error",
                    )
                    return Redirect("/ceasefires")
                raise
        _set_flash(request, f"雨棚停火令已挂出（{site_name}），该窑场暂停登记焖烧班次", "ok")
        return Redirect("/ceasefires")

    @post("/ceasefires/{order_id:int}/lift")
    async def lift(self, request: Request, order_id: int) -> Redirect:
        if not request.user:
            return Redirect("/login")
        if request.user.role != "admin":
            _set_flash(request, "仅管理员可收雨棚停火令", "error")
            return Redirect("/ceasefires")
        async with SessionLocal() as db:
            order = (
                await db.execute(
                    select(Ceasefire)
                    .where(Ceasefire.id == order_id)
                    .options(selectinload(Ceasefire.site))
                )
            ).scalar_one_or_none()
            if not order:
                _set_flash(request, "停火令不存在", "error")
                return Redirect("/ceasefires")
            if not order.is_open:
                _set_flash(request, "该停火令已经收过令", "error")
                return Redirect("/ceasefires")
            order.lifted_at = utcnow()
            order.lifted_by = request.user.id
            await db.commit()
            site_name = order.site.name
        _set_flash(request, f"雨棚停火令已收（{site_name}），可恢复登记焖烧班次", "ok")
        return Redirect("/ceasefires")
