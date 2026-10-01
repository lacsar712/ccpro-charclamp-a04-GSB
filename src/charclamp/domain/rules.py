"""炭窑焖烧志业务规则。"""

from __future__ import annotations

from charclamp.domain.models import BurnShift, Ceasefire, Clamp

MIN_PEAK_TEMP_FOR_DRAWN = 400.0


class RuleError(ValueError):
    """业务规则校验失败。"""


def latest_shift_for_clamp(clamp: Clamp) -> BurnShift | None:
    if not clamp.shifts:
        return None
    return max(clamp.shifts, key=lambda s: s.started_at)


def ceasefire_block_message(site_name: str | None = None) -> str:
    """雨棚停火令生效时登记焖烧班次的统一中文拦截说明。"""
    where_site = f"窑场「{site_name}」" if site_name else "该窑场"
    return (
        f"雨棚停火：{where_site}停火令生效中，禁止再登记任何焖烧班次，"
        "已有时间轴只读；请等待管理员收令后再登记。"
        "（出炭与改回已码窑不受停火令影响）"
    )


def assert_can_register_shift(clamp: Clamp, ceasefire: Ceasefire | None) -> None:
    """登记焖烧班次前的停火令校验：该窑所在窑场存在未收令即拒绝。"""
    if ceasefire is not None and ceasefire.is_open:
        site_name = clamp.site.name if clamp.site is not None else None
        raise RuleError(ceasefire_block_message(site_name))


def can_mark_clamp_drawn(clamp: Clamp) -> tuple[bool, str]:
    """
    炭窑转为「已出炭」(drawn) 的前提：
    最近一条焖烧班次的峰值温度已记录，且 >= 400℃。
    """
    latest = latest_shift_for_clamp(clamp)
    if latest is None:
        return False, "该窑尚无焖烧班次，不能标记为已出炭"
    if latest.peak_temp_c is None:
        return False, "最近班次尚未记录峰值温度，不能标记为已出炭"
    if latest.peak_temp_c < MIN_PEAK_TEMP_FOR_DRAWN:
        return (
            False,
            f"最近班次峰值温度 {latest.peak_temp_c}℃ 低于 {MIN_PEAK_TEMP_FOR_DRAWN:.0f}℃，不能标记为已出炭",
        )
    return True, ""


def assert_can_set_clamp_status(clamp: Clamp, new_status: str) -> None:
    allowed = {Clamp.STATUS_STACKED, Clamp.STATUS_BURNING, Clamp.STATUS_DRAWN}
    if new_status not in allowed:
        raise RuleError(f"无效状态：{new_status}")
    if new_status == Clamp.STATUS_DRAWN:
        ok, msg = can_mark_clamp_drawn(clamp)
        if not ok:
            raise RuleError(msg)
