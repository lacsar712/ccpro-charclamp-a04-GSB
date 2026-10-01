from __future__ import annotations

from datetime import timedelta

from charclamp.domain.models import BurnShift, Ceasefire, Clamp, Site, User, utcnow
from charclamp.infra.db import SyncSessionLocal
from charclamp.infra.security import hash_password


def seed_demo() -> None:
    with SyncSessionLocal() as session:
        admin = session.query(User).filter_by(username="admin").first()
        if not admin:
            admin = User(username="admin", role="admin", password_hash=hash_password("123456"))
            session.add(admin)
            session.flush()
        else:
            admin.password_hash = hash_password("123456")
            admin.role = "admin"

        worker = session.query(User).filter_by(username="worker").first()
        if not worker:
            worker = User(username="worker", role="worker", password_hash=hash_password("123456"))
            session.add(worker)
        else:
            worker.password_hash = hash_password("123456")
            worker.role = "worker"

        existing_site = session.query(Site).first()
        if existing_site:
            _ensure_open_ceasefire(session, existing_site, admin)
            session.commit()
            return

        site = Site(name="乌石岗焖烧坞", location="河谷台地北侧", notes="青冈为主，夜班闷窑")
        session.add(site)
        session.flush()

        c1 = Clamp(site=site, code="坞东-甲", status=Clamp.STATUS_BURNING, wood_species="青冈")
        c2 = Clamp(site=site, code="坞东-乙", status=Clamp.STATUS_STACKED, wood_species="松木")
        c3 = Clamp(site=site, code="河沿-丙", status=Clamp.STATUS_DRAWN, wood_species="栎木")
        session.add_all([c1, c2, c3])
        session.flush()

        now = utcnow()
        session.add_all(
            [
                BurnShift(
                    clamp=c1,
                    started_at=now - timedelta(hours=10),
                    peak_temp_c=455.0,
                    charcoal_grade="A",
                    notes="峰值已过，可出炭",
                ),
                BurnShift(
                    clamp=c2,
                    started_at=now - timedelta(hours=3),
                    peak_temp_c=None,
                    charcoal_grade="B",
                    notes="刚点火，未测峰值",
                ),
                BurnShift(
                    clamp=c3,
                    started_at=now - timedelta(days=2),
                    peak_temp_c=520.0,
                    charcoal_grade="A+",
                    notes="已出炭班次",
                ),
            ]
        )

        # 种子挂一条未收停火令（演示雨棚停火拦截）。
        _ensure_open_ceasefire(session, site, admin)
        session.commit()


def _ensure_open_ceasefire(session, site: Site, admin: User) -> None:
    """幂等：该窑场若无任何停火令，则补一条未收令；已收令或已存在则不动。"""
    has_any = (
        session.query(Ceasefire)
        .filter(Ceasefire.site_id == site.id)
        .first()
    )
    if has_any is not None:
        return
    now = utcnow()
    session.add(
        Ceasefire(
            site=site,
            effective_at=now - timedelta(hours=2),
            planned_lift_date=(now + timedelta(days=1)).date(),
            rain_summary="午后雷阵雨持续，雨棚湿滑排水告急，暂停一切焖烧登记",
            issued_by=admin.id,
        )
    )
