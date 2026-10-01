from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from litestar import Controller, MediaType, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from charclamp.domain.models import BurnShift, CeasefireOrder, Clamp, Site, User, utcnow
from charclamp.domain.rules import (
    RuleError,
    assert_can_set_clamp_status,
    assert_not_in_ceasefire,
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


def _is_admin(user: Any) -> bool:
    return bool(user is not None and getattr(user, "role", None) == "admin")


def _as_utc(dt: datetime) -> datetime:
    """表单 datetime-local 不带时区，按 UTC 补 tzinfo 后再写 timestamptz 列。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def _open_ceasefire_map(db) -> dict[int, CeasefireOrder]:
    """site_id -> 该窑场当前未收令（部分唯一索引保证每窑场至多一条）。"""
    rows = (
        await db.execute(
            select(CeasefireOrder)
            .where(CeasefireOrder.lifted_at.is_(None))
            .options(selectinload(CeasefireOrder.site), selectinload(CeasefireOrder.issuer))
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
        open_by_site = await _open_ceasefire_map(db)
        site_name = clamps[0].site.name if clamps else "乌石岗焖烧坞"

    if clamp_id is not None:
        banner_sites = {c.site_id for c in clamps if c.id == clamp_id}
    else:
        banner_sites = {c.site_id for c in clamps}
    banner_orders = [order for site_id, order in open_by_site.items() if site_id in banner_sites]
    return {
        "clamps": clamps,
        "shifts": shifts,
        "active_clamp_id": clamp_id,
        "status_labels": STATUS_LABELS,
        "site_name": site_name,
        "ceasefire_by_site": open_by_site,
        "banner_orders": banner_orders,
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
                "is_admin": _is_admin(request.user),
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
                "is_admin": _is_admin(request.user),
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
                        select(Clamp)
                        .options(selectinload(Clamp.site))
                        .order_by(Clamp.code)
                    )
                )
                .scalars()
                .all()
            )
            open_by_site = await _open_ceasefire_map(db)
        blocked_site_ids = set(open_by_site)
        return Template(
            template_name="partials/drawer_shift.html",
            context={
                "clamps": clamps,
                "preselect_clamp_id": clamp_id,
                "user": request.user,
                "ceasefire_by_site": open_by_site,
                "blocked_site_ids": blocked_site_ids,
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
                .options(
                    selectinload(Clamp.shifts),
                    selectinload(Clamp.site),
                )
            )
            clamp = result.scalar_one_or_none()
            if not clamp:
                return Redirect("/")
            ceasefire_order = (
                await db.execute(
                    select(CeasefireOrder)
                    .where(
                        CeasefireOrder.site_id == clamp.site_id,
                        CeasefireOrder.lifted_at.is_(None),
                    )
                    .options(selectinload(CeasefireOrder.issuer))
                )
            ).scalar_one_or_none()
        can_drawn, drawn_msg = can_mark_clamp_drawn(clamp)
        return Template(
            template_name="partials/drawer_clamp.html",
            context={
                "clamp": clamp,
                "status_labels": STATUS_LABELS,
                "can_drawn": can_drawn,
                "drawn_msg": drawn_msg,
                "user": request.user,
                "ceasefire_order": ceasefire_order,
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
        redirect_to = "/"
        try:
            clamp_id = int(data["clamp_id"])
        except (KeyError, TypeError, ValueError):
            _set_flash(request, "缺少有效的炭窑，班次未登记", "error")
            return Redirect(redirect_to)
        redirect_to = f"/?clamp_id={clamp_id}"
        started_raw = data.get("started_at") or ""
        started_at = _as_utc(
            datetime.fromisoformat(started_raw) if started_raw else datetime.utcnow()
        )
        peak_raw = (data.get("peak_temp_c") or "").strip()
        peak = float(peak_raw) if peak_raw else None
        async with SessionLocal() as db:
            clamp = (
                await db.execute(
                    select(Clamp)
                    .where(Clamp.id == clamp_id)
                    .options(selectinload(Clamp.site))
                )
            ).scalar_one_or_none()
            if not clamp:
                _set_flash(request, "炭窑不存在，班次未登记", "error")
                return Redirect("/")

            # 雨棚停火令：服务端硬拦截，不能靠前端藏按钮
            open_order = (
                await db.execute(
                    select(CeasefireOrder).where(
                        CeasefireOrder.site_id == clamp.site_id,
                        CeasefireOrder.lifted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            try:
                assert_not_in_ceasefire([open_order] if open_order else [], clamp.site.name)
            except RuleError as exc:
                _set_flash(request, str(exc), "error")
                return Redirect(redirect_to)

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
        return Redirect(redirect_to)


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
            # 出炭与改回已码窑不受雨棚停火令影响，仍只走原峰值规则
            try:
                assert_can_set_clamp_status(clamp, new_status)
                clamp.status = new_status
                await db.commit()
                _set_flash(request, f"窑 {clamp.code} 状态已更新", "ok")
            except RuleError as exc:
                _set_flash(request, str(exc), "error")
        return Redirect(f"/?clamp_id={clamp_id}")


class CeasefireController(Controller):
    path = "/ceasefire"
    tags = ["ceasefire"]

    @get(["", "/"], media_type=MediaType.HTML)
    async def ceasefire_page(self, request: Request) -> Template | Redirect:
        if not request.user:
            return Redirect("/login")
        flash, flash_cat = _pop_flash(request)
        async with SessionLocal() as db:
            sites = list(
                (await db.execute(select(Site).order_by(Site.name))).scalars().all()
            )
            active_orders = list(
                (
                    await db.execute(
                        select(CeasefireOrder)
                        .where(CeasefireOrder.lifted_at.is_(None))
                        .options(
                            selectinload(CeasefireOrder.site),
                            selectinload(CeasefireOrder.issuer),
                        )
                        .order_by(CeasefireOrder.effective_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            history_orders = list(
                (
                    await db.execute(
                        select(CeasefireOrder)
                        .where(CeasefireOrder.lifted_at.is_not(None))
                        .options(
                            selectinload(CeasefireOrder.site),
                            selectinload(CeasefireOrder.issuer),
                            selectinload(CeasefireOrder.lifter),
                        )
                        .order_by(CeasefireOrder.lifted_at.desc())
                        .limit(10)
                    )
                )
                .scalars()
                .all()
            )
        active_site_ids = {order.site_id for order in active_orders}
        return Template(
            template_name="ceasefire.html",
            context={
                "sites": sites,
                "active_orders": active_orders,
                "history_orders": history_orders,
                "active_site_ids": active_site_ids,
                "user": request.user,
                "is_admin": _is_admin(request.user),
                "flash": flash,
                "flash_cat": flash_cat,
            },
        )

    @post("/issue")
    async def issue_order(
        self,
        request: Request,
        data: dict[str, Any] = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        if not request.user:
            return Redirect("/login")
        if not _is_admin(request.user):
            # 非管理员禁止挂令：不执行任何写入
            _set_flash(request, "仅管理员可挂雨棚停火令", "error")
            return Redirect("/ceasefire")
        try:
            site_id = int(data.get("site_id"))
        except (TypeError, ValueError):
            _set_flash(request, "请选择要挂令的窑场", "error")
            return Redirect("/ceasefire")
        rain_summary = (data.get("rain_summary") or "").strip()
        if not rain_summary:
            _set_flash(request, "请填写降雨摘要", "error")
            return Redirect("/ceasefire")
        effective_raw = (data.get("effective_at") or "").strip()
        try:
            effective_at = (
                _as_utc(datetime.fromisoformat(effective_raw))
                if effective_raw
                else utcnow()
            )
        except ValueError:
            _set_flash(request, "生效时刻格式无效", "error")
            return Redirect("/ceasefire")
        planned_raw = (data.get("planned_lift_date") or "").strip()
        try:
            planned_lift_date = (
                date.fromisoformat(planned_raw)
                if planned_raw
                else effective_at.date()
            )
        except ValueError:
            _set_flash(request, "计划收令日格式无效", "error")
            return Redirect("/ceasefire")

        async with SessionLocal() as db:
            site = (
                await db.execute(select(Site).where(Site.id == site_id))
            ).scalar_one_or_none()
            if site is None:
                _set_flash(request, "窑场不存在，未挂令", "error")
                return Redirect("/ceasefire")
            existing = (
                await db.execute(
                    select(CeasefireOrder.id).where(
                        CeasefireOrder.site_id == site_id,
                        CeasefireOrder.lifted_at.is_(None),
                    )
                )
            ).first()
            if existing is not None:
                _set_flash(
                    request,
                    f"「{site.name}」已有未收的雨棚停火令，收令前不可再挂第二条",
                    "error",
                )
                return Redirect("/ceasefire")

            order = CeasefireOrder(
                site_id=site_id,
                effective_at=effective_at,
                planned_lift_date=planned_lift_date,
                rain_summary=rain_summary,
                issued_by=request.user.id,
            )
            db.add(order)
            try:
                await db.commit()
            except IntegrityError:
                # 两名管理员同时挂令：部分唯一索引只放行一条
                await db.rollback()
                _set_flash(
                    request,
                    f"「{site.name}」已有未收的雨棚停火令（并发挂令仅落一条），本条未生效",
                    "error",
                )
                return Redirect("/ceasefire")
        _set_flash(request, f"已对「{site.name}」挂雨棚停火令", "ok")
        return Redirect("/ceasefire")

    @post("/{order_id:int}/lift")
    async def lift_order(self, request: Request, order_id: int) -> Redirect:
        if not request.user:
            return Redirect("/login")
        if not _is_admin(request.user):
            _set_flash(request, "仅管理员可收雨棚停火令", "error")
            return Redirect("/ceasefire")
        async with SessionLocal() as db:
            order = (
                await db.execute(
                    select(CeasefireOrder)
                    .where(CeasefireOrder.id == order_id)
                    .options(selectinload(CeasefireOrder.site))
                )
            ).scalar_one_or_none()
            if order is None:
                _set_flash(request, "停火令不存在，未收令", "error")
                return Redirect("/ceasefire")
            if order.lifted_at is not None:
                _set_flash(request, "该停火令已收，无需重复收令", "error")
                return Redirect("/ceasefire")
            order.lifted_at = utcnow()
            order.lifted_by = request.user.id
            await db.commit()
            site_name = order.site.name
        _set_flash(request, f"「{site_name}」雨棚停火令已收，可恢复登记班次", "ok")
        return Redirect("/ceasefire")
