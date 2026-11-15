"""English / Urdu strings for navigation and the core journey.

Only interface chrome is translated so far (a foundation for Constraint Card D4,
Urdu/RTL).  Urdu text should be reviewed by a native speaker before user
testing.  Missing keys fall back to English.
"""
from __future__ import annotations

STRINGS: dict[str, tuple[str, str]] = {
    "app.name": ("Budget Dashboard", "بجٹ ڈیش بورڈ"),
    "skip": ("Skip to main content", "مرکزی مواد پر جائیں"),
    "nav.dashboard": ("Dashboard", "ڈیش بورڈ"),
    "nav.transactions": ("Transactions", "لین دین"),
    "nav.budgets": ("Budgets", "بجٹ"),
    "nav.bills": ("Recurring", "باقاعدہ بل"),
    "nav.goals": ("Goals", "بچت کے اہداف"),
    "nav.forecast": ("Forecast", "پیش گوئی"),
    "nav.whatif": ("What-if", "اگر ایسا ہو"),
    "nav.import": ("Import", "فائل درآمد"),
    "nav.export": ("Export", "برآمد"),
    "nav.history": ("Activity", "سرگرمی"),
    "nav.settings": ("Settings", "ترتیبات"),
    "nav.coach": ("Clients", "کلائنٹس"),
    "nav.more": ("More", "مزید"),
    "lens": ("Detail level", "تفصیل کی سطح"),
    "lens.summary": ("Summary", "خلاصہ"),
    "lens.standard": ("Standard", "معیاری"),
    "lens.detailed": ("Detailed", "تفصیلی"),
    "explain": ("How is this worked out?", "یہ کیسے حساب ہوا؟"),
    "demo": ("Demo with made-up data. Not connected to any bank. Not financial advice.",
             "فرضی ڈیٹا کے ساتھ ڈیمو۔ کسی بینک سے منسلک نہیں۔ مالی مشورہ نہیں۔"),
    "switch_profile": ("Switch profile", "پروفائل بدلیں"),
    "free_to_spend": ("Free to spend", "خرچ کے لیے دستیاب"),
    "balance_today": ("Balance today", "آج کا بیلنس"),
    "forecast_balance": ("Forecast balance", "متوقع بیلنس"),
    "needs_attention": ("Needs attention", "توجہ طلب"),
    "save": ("Save", "محفوظ کریں"),
    "cancel": ("Cancel", "منسوخ"),
    "actual": ("Actual", "حقیقی"),
    "forecast": ("Forecast", "پیش گوئی"),
}


def translate(key: str, language: str = "en") -> str:
    en, ur = STRINGS.get(key, (key, key))
    return ur if language == "ur" else en
