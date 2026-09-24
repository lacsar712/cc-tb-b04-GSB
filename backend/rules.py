import re


def weigh(aroma: float, taste: float, liquor: float) -> tuple[str, str, float]:
    score = round(aroma * 0.3 + taste * 0.5 + liquor * 0.2, 2)
    if score >= 7:
        return "通过", "加权分达到放行线", score
    return "不通过", "加权分低于放行线", score


def parse_invitees(text: str) -> list[str]:
    """把建会表单里的审评员名列表（逗号、顿号或空白分隔）解析成去重后的名单。"""
    return sorted({name for name in re.split(r"[，,、;；\s]+", text) if name})
