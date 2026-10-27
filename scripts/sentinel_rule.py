#!/usr/bin/env python3
"""Sentinel's model-free fallback: score an email when the scoring model is down.

Used only by `scripts/sentinel.py` with `llm.provider: qwen`, and only while the
model does not answer. It reproduces, at runtime, the feature extraction and the
scoring of a logistic rule fitted offline on the operator's own mail history.
The fitted weights are derived from private mail, so they are NEVER in this
repository: they are read at runtime from the data overlay, by default
`<data-config>/sentinel-rule-weights.json` (registry name `data-config`). With no
weights file the rule is unavailable and Sentinel says so; nothing is invented.

What this module carries is generic: word lists (English and Russian) and the
arithmetic. The weights file carries everything fitted or personal: the weight,
mean and standard deviation per feature, the probability cut-offs that map to a
1-10 score, and an optional `runtime` block with the operator's own domains, the
spellings of their name in capitals, and the weekend days the fit used.

The score, as the fit defined it:
    z_f   = (x_f - mean_f) / sd_f          per feature
    p     = sigmoid(intercept + sum w_f z_f)
    score = 1..6 by `low_cuts` below `p_wake`, 7..10 by `high_cuts` at or above it
    a message that writes the operator's name in capitals, from an external,
    non-government sender, in a thread they never wrote in: score capped at 3.

A feature this runtime cannot compute is imputed at its training mean, so it
contributes exactly zero rather than a guess. `RuleModel.missing` names them, and
Sentinel logs that list once, so the narrower coverage is said out loud.

Usage (a self-check over a weights file, printing only counts):
    python scripts/sentinel_rule.py check [--weights PATH]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

WEIGHTS_FILENAME = "sentinel-rule-weights.json"

# Features the fit discarded (`DROP` in the fitting tool), and the two that were
# label leaks or constant per channel. Never scored even if a file names them.
_DROPPED = {"hour_local", "n_recipients", "sender_prior_items", "mail_found", "is_email"}

# ---------------------------------------------------------------- lexicons
# First lexicon: hand-written. The fit decides which of these carry weight.
LEX = {
    "urgent": r"\b(urgent|asap|immediately|right away|critical|emergency)\b|срочн|немедленн|критичн",
    "deadline": r"\b(deadline|due|overdue|expires?|expiring|by (eod|cob|end of (the )?day)|final notice|last chance)\b"
                r"|дедлайн|срок|просроч|до конца дня|истекает|последн(ий|ее) (день|напоминание)",
    "today_tomorrow": r"\b(today|tonight|tomorrow|this (morning|afternoon|evening))\b|сегодня|завтра|вечером",
    "money": r"\b(invoice|payment|paid|pay|wire|transfer|refund|bank|loan|mortgage)\b"
             r"|оплат|счёт|счет|платеж|платёж|перевод|банк|ипотек",
    "contract": r"\b(contract|agreement|nda|mnda|mou|loi|sign(ed|ature)?|po\b|purchase order|tender|rfp|rfq|rfx"
                r"|proposal|quote|quotation)\b|договор|контракт|подпис|соглашени|тендер|предложени|коммерческ",
    "ask": r"\b(please (confirm|advise|reply|respond|review|approve|call)|can you|could you|let me know"
           r"|waiting for your|your (approval|decision|feedback))\b"
           r"|подтверди|ответь|ответьте|перезвон|позвони|жду (ответа|твоего|вашего)|согласуй|посмотри",
    "meeting": r"\b(meeting|call|zoom|teams|demo|visit|invitation|invite)\b|встреч|созвон|звонок|демо",
    "marketing": r"\b(unsubscribe|newsletter|webinar|sale|% off|discount|deals?|offer ends|promo|subscribe)\b"
                 r"|рассылк|скидк|акци|вебинар",
}
LEX_RE = {k: re.compile(v, re.I) for k, v in LEX.items()}
DATE_RE = re.compile(r"\b(\d{1,2}[./-]\d{1,2}([./-]\d{2,4})?|\d{4}-\d{2}-\d{2}|(mon|tue|wed|thu|fri|sat|sun)[a-z]*day"
                     r"|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2})\b"
                     r"|понедельник|вторник|сред[ау]|четверг|пятниц|суббот|воскресень", re.I)
AMOUNT_RE = re.compile(r"([$€£]\s?\d|\d[\d,. ]*\s?(usd|eur|aed|gbp|\$|€|k\b|m\b|млн|тыс|руб|дирхам))", re.I)
AUTO_RE = re.compile(r"(no-?reply|do-?not-?reply|notification|newsletter|mailer|marketing|news@|info@|hello@"
                     r"|support@|updates?@|digest)", re.I)
REPLY_RE = re.compile(r"^\s*(re|ответ|отв)\s*:", re.I)
GOV_RE = re.compile(r"(^|\.)gov(\.[a-z]{2,3})?$|\.gov\.")
ROLE_LOCALS = ("accounting", "info", "sales", "support", "admin", "office")


def _join(pattern: str) -> re.Pattern:
    """Compile a multi-line pattern WITHOUT re.X: several carry literal spaces."""
    return re.compile(re.sub(r"\n\s*", "", pattern), re.I)


# Second lexicon, positive and negative signals in both languages.
POS = {
    "contract_stage": _join(r"""
        \b(contract|agreement|nda|mnda|mou|loi|term\s?sheet|purchase\s?order|po\s?#\s?\d+|
           tender|rfp|rfq|rfi|statement\s?of\s?work|\bsow\b|commercial\s?proposal|quotation)\b
        |договор|контракт|соглашени|тендер|коммерческ\w* предложени|\bкп\b|
         спецификаци|приложени[ея] к договору|доп\.?\s?соглашени|запрос предложени"""),
    "signature": _join(r"""
        \b(countersign|e-?sign|docusign|sign(ing)?\s?(the\s?)?(contract|agreement|nda|po)|
           please\s?sign|for\s?signature)\b
        |подпис(ь|ание|ать|ать\s?проект|анием)|подпиши(те)?|контрподпис|эцп|
         электронн\w*\s?подпис|на подпись|прошу подписать"""),
    "payment_due": _join(r"""
        \b(invoice\s?(no\.?|number|#)?\s?\d+|payment\s?(due|overdue|received|confirmation)|
           outstanding\s?(invoice|balance|payment)|remittance|wire\s?transfer|
           advance\s?payment|prepayment|settle\s?(the\s?)?(invoice|payment)|past\s?due)\b
        |сч[её]т на оплату|оплат(ите|ить|а)\s?(сч[её]т|по сч[её]ту)|
         просроч\w*\s?(плат|оплат)|задолженност|авансов\w*\s?плат|
         акт\s?(выполненных|сверки)|сверк\w*\s?взаиморасч"""),
    "decision_ask": _join(r"""
        \b(your\s?(approval|decision|confirmation|go-?ahead|sign-?off)|
           please\s?(approve|confirm|review|advise)|
           awaiting\s?your|waiting\s?(for|on)\s?your|
           can\s?you\s?(confirm|approve|review)|could\s?you\s?(confirm|approve)|
           let\s?(me|us)\s?know\s?(by|if|whether))\b
        |жду\s?(вашего|твоего)\s?(ответа|решения|подтверждения|согласования)|
         подтверди(те)?|согласуй(те)?|на\s?согласование|нужно\s?ваше\s?(решение|согласование)|
         прошу\s?(подтвердить|согласовать|рассмотреть)"""),
    "meeting_change": _join(r"""
        \b(reschedul\w*|postpon\w*|cancel\w*\s?(the\s?|our\s?)?(meeting|call|visit)|
           move\s?(the\s?|our\s?)?(meeting|call)|bring\s?forward|
           (meeting|call)\s?moved|new\s?time\s?for|change\s?of\s?time)\b
        |перенос\w*\s?(встреч|звонк|созвон)|перенес(ти|ём|ем|ите|и|ена|ено)|
         отмен\w*\s?(встреч|звонк|созвон)|изменилось\s?время|другое\s?время|
         встреча\s?перенесена"""),
    "deadline_real": _join(r"""
        \b(deadline\s?(for|is)|due\s?(date|by)|respond\s?by|reply\s?by|
           submission\s?(deadline|date)|delivery\s?(date|by)|submit\s?by)\b
        |дедлайн|срок\s?(сдачи|поставки|предоставления|оплаты|ответа|подачи)|
         не\s?позднее|крайний\s?срок|до\s?\d{1,2}[./]\d{1,2}"""),
}
NEG = {
    "urgency_theatre": _join(r"""
        \b(act\s?now|limited\s?(time|offer|seats?|availability)|don'?t\s?miss|
           last\s?chance|final\s?(notice|call|reminder)|offer\s?(ends|expires)|
           expires?\s?(soon|today|tomorrow)|hurry|today\s?only|only\s?\d+\s?(left|days|hours)|
           ending\s?soon)\b
        |успей(те)?|только\s?сегодня|последн\w*\s?(день|возможность|шанс)|
         ограниченное\s?предложени|не\s?пропусти(те)?|акция\s?заканчивается|
         предложение\s?истекает|осталось\s?(мест|\d+)"""),
    "anon_greeting": _join(r"""
        \bdear\s?(sir|madam|friend|valued\s?customer|customer|partner|colleague)s?\b
        |дорогой\s?друг|уважаем(ый|ая)\s?(господин|госпожа|клиент|абонент|партн[её]р)\b"""),
    "invest_pitch": _join(r"""
        \b(investment\s?opportunit\w*|guaranteed\s?(returns?|profit|income)|high\s?yield|
           risk-?free|passive\s?income|double\s?your|trading\s?(signals?|bot)|
           forex|crypto(currency)?|earn\s?up\s?to)\b
        |инвестиционн\w*\s?предложени|гарантированн\w*\s?(прибыль|доход)|
         пассивн\w*\s?доход|высок\w*\s?доходност|крипто|заработ\w*\s?от\s?\d"""),
    "fear_hook": _join(r"""
        \b(account\s?(suspended|locked|compromised|restricted)|verify\s?your\s?(account|identity)|
           unusual\s?(activity|sign-?in)|password\s?(expir\w*|reset)|security\s?alert|
           confirm\s?your\s?(details|identity|account))\b
        |аккаунт\s?(заблокирован|взломан|ограничен)|подтвердите\s?(свои\s?данные|личность)|
         подозрительн\w*\s?активност|срок\s?действия\s?пароля|ваш\s?аккаунт"""),
    "marketing": _join(r"""
        \b(unsubscribe|newsletter|webinar|\d+\s?%\s?off|discount|promo\s?code|
           deals?|subscribe\s?now|free\s?(webinar|trial|consultation))\b
        |рассылк|скидк|акци|вебинар|промокод|распродаж|бесплатн\w*\s?(вебинар|консультаци)"""),
}

RELATIONSHIPS = ("prospect", "customer", "partner", "investor", "tribe", "government",
                 "vendor", "ecosystem", "inactive")


def lexicon(text: str) -> dict:
    t = text or ""
    f = {f"lex_{k}": int(bool(r.search(t))) for k, r in LEX_RE.items()}
    f["has_date"] = int(bool(DATE_RE.search(t)))
    f["has_amount"] = int(bool(AMOUNT_RE.search(t)))
    for k, r in POS.items():
        f[f"k_pos_{k}"] = int(bool(r.search(t)))
    for k, r in NEG.items():
        f[f"k_neg_{k}"] = int(bool(r.search(t)))
    f["x_amount_payment"] = f["has_amount"] * f["k_pos_payment_due"]
    f["x_date_ask"] = f["has_date"] * f["k_pos_decision_ask"]
    return f


# ---------------------------------------------------------------- features

class RuleContext:
    """What the features need beyond the message itself, gathered by the caller.

    `crm_index` maps a lower-cased address to its CRM record
    (`scripts/utils/crm.contact_index_by_email`). `wrote_in_conversation(conv_id,
    before)` answers from the Sent folder. `sender_times` lists when earlier items
    from the same sender were seen.

    None for `crm_index` or `wrote_in_conversation` means the source could not be
    read, and then its features are left OUT, so they are imputed at the training
    mean and contribute nothing. An empty source is different: it is a real
    answer ("not a known contact") and scores as one. Reading an unreadable CRM
    as "nobody is a known contact" pushed every score down.
    """

    def __init__(self, *, operator_email: str, own_domains, name_caps, weekend_days, tz,
                 crm_index=None, vip=False, wrote_in_conversation=None, sender_times=()):
        self.operator_email = (operator_email or "").strip().lower()
        self.own_domains = {d.strip().lower() for d in own_domains if d and d.strip()}
        self.name_caps = [n for n in name_caps if n and n.strip()]
        self.weekend_days = set(weekend_days)
        self.tz = tz
        self.crm_index = crm_index
        self.vip = bool(vip)
        self.wrote_in_conversation = wrote_in_conversation
        self.sender_times = list(sender_times)


def _caps_re(names) -> re.Pattern | None:
    alts = "|".join(re.escape(n.strip()) for n in names if n and n.strip())
    if not alts:
        return None
    # Case-SENSITIVE on purpose: the signal is the name written in capitals.
    return re.compile(rf"(?<![A-Za-zА-Яа-яЁё])({alts})(?![A-Za-zА-Яа-яЁё])")


def email_features(item: dict, ctx: RuleContext) -> dict:
    """The fit's email features for one Sentinel item.

    `item` is what `EmailSource.check_new` builds; `item["rule_meta"]` carries the
    header fields read at fetch time (`received`, `conversation_id`, `to`, `cc`,
    `has_attachments`, `is_reply`, `list_unsubscribe`, `importance`).
    """
    meta = item.get("rule_meta") or {}
    sender = (item.get("sender_email") or "").strip().lower()
    dom = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    subject = str(item.get("subject") or "")
    text = subject + "\n" + str(item.get("body") or "")[:1500]
    try:
        received = datetime.fromisoformat(meta["received"])
    except (KeyError, TypeError, ValueError):
        received = datetime.now(timezone.utc)
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    local = received.astimezone(ctx.tz)

    to = [str(a).strip().lower() for a in meta.get("to") or [] if a]
    operator_in_to = bool(ctx.operator_email) and ctx.operator_email in to
    internal = int(dom in ctx.own_domains)
    gov = int(bool(GOV_RE.search(dom)))
    if ctx.wrote_in_conversation is None:
        prior = 0  # unknown; the feature itself is dropped below
    else:
        prior = int(bool(meta.get("conversation_id"))
                    and bool(ctx.wrote_in_conversation(meta.get("conversation_id"), received)))

    f = {
        "weekend": int(local.weekday() in ctx.weekend_days),
        "night": int(local.hour < 7 or local.hour >= 23),
        "operator_in_to": int(operator_in_to),
        "sole_to": int(len(to) == 1 and operator_in_to),
        "has_attachments": int(bool(meta.get("has_attachments"))),
        "is_reply": int(bool(meta.get("is_reply")) or bool(REPLY_RE.match(subject))),
        "list_unsubscribe": int(bool(meta.get("list_unsubscribe"))),
        "importance_high": int(str(meta.get("importance") or "") == "High"),
        "internal": internal,
        "automated": int(bool(AUTO_RE.search(sender))),
        "addressed_role": int(any("@" in a and a.split("@")[0] in ROLE_LOCALS
                                  and a.rsplit("@", 1)[-1] in ctx.own_domains for a in to)),
        "his_prior_in_conv": prior,
        "gov_sender": gov,
        "vip": int(ctx.vip),
    }
    caps = _caps_re(ctx.name_caps)
    f["k_neg_name_caps"] = int(bool(caps and caps.search(text)) and not (internal or gov or prior))
    if ctx.wrote_in_conversation is None:
        del f["his_prior_in_conv"]

    if ctx.crm_index is not None:
        crm = ctx.crm_index.get(sender) if sender else None
        rtype = str((crm or {}).get("relationship_type") or (crm or {}).get("type") or "").strip().lower()
        f["crm_known"] = int(crm is not None)
        for r in RELATIONSHIPS:
            f[f"rel_{r}"] = int(bool(rtype) and rtype.startswith(r))

    week_ago = received - timedelta(days=7)
    f["sender_items_7d"] = min(sum(1 for t in ctx.sender_times if week_ago <= t < received), 50)
    f.update(lexicon(text))
    return f


# Features the runtime above produces. Anything else the weights file names is
# imputed at its training mean (it contributes zero) and reported in `missing`.
COMPUTED = set(email_features({"rule_meta": {}}, RuleContext(
    operator_email="", own_domains=(), name_caps=(), weekend_days=(), tz=timezone.utc,
    crm_index={}, wrote_in_conversation=lambda conv_id, before: False)))


# ---------------------------------------------------------------- the model

class RuleUnavailable(RuntimeError):
    """No usable weights file. Sentinel then has no fallback, and says so."""


class RuleModel:
    def __init__(self, doc: dict, source: Path | None = None):
        try:
            email = doc["email"]
            self.intercept = float(email["intercept"])
            self.p_wake = float(email["p_wake"])
            self.low_cuts = [float(v) for v in email["low_cuts"]]
            self.high_cuts = [float(v) for v in email["high_cuts"]]
            self.features = {
                name: (float(v["weight"]), float(v["mean"]), float(v["sd"]) or 1.0, str(v.get("words") or name))
                for name, v in email["features"].items() if name not in _DROPPED
            }
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise RuleUnavailable(f"weights file {source or ''} is not in the expected shape: {exc!r}") from exc
        runtime = doc.get("runtime") or {}
        self.own_domains = [str(d) for d in runtime.get("own_domains") or []]
        self.name_caps = [str(n) for n in runtime.get("name_caps") or []]
        self.weekend_days = [int(d) for d in runtime.get("weekend_days") or []]
        self.source = source
        self.fitted_at = doc.get("fitted_at")
        self.missing = sorted(n for n, (w, *_rest) in self.features.items()
                              if w != 0 and n not in COMPUTED)

    def _transform(self, raw: dict) -> dict:
        """The fit's own `vec()` transformation of a raw feature dict."""
        out = {}
        for k, v in raw.items():
            if k in _DROPPED or v is None:
                continue
            if k == "sender_items_7d":
                out[k] = math.log1p(v)
            else:
                out[k] = float(v)
        return out

    def probability(self, raw: dict) -> tuple[float, list]:
        x = self._transform(raw)
        logit = self.intercept
        parts = []
        for name, (w, mean, sd, words) in self.features.items():
            if name not in x:
                continue  # imputed at the training mean: contributes zero
            c = w * (x[name] - mean) / sd
            logit += c
            parts.append((c, words))
        p = 1.0 / (1.0 + math.exp(-logit))
        return p, sorted(parts, key=lambda t: -abs(t[0]))

    def to_score(self, p: float) -> int:
        if p < self.p_wake:
            return 1 + sum(1 for c in self.low_cuts if p >= c)
        return 7 + sum(1 for c in self.high_cuts if p >= c)

    def score(self, raw: dict) -> tuple[int, str]:
        """(1-10 score, a plain-words line naming what pushed it up and down)."""
        p, parts = self.probability(raw)
        s = self.to_score(p)
        if raw.get("k_neg_name_caps"):
            s = min(s, 3)
        up = [w for c, w in parts if c > 0.05][:3]
        down = [w for c, w in parts if c < -0.05][:2]
        why = []
        if up:
            why.append("up: " + ", ".join(up))
        if down:
            why.append("down: " + ", ".join(down))
        return s, "; ".join(why) or "no strong signal"


def default_weights_path() -> Path:
    from scripts.utils.data_path import data_path
    return data_path("data-config") / WEIGHTS_FILENAME


def load_rule(path: Path | str | None = None) -> RuleModel:
    """The rule from a weights file; a relative path is taken under `data-config`."""
    if path is None or str(path).strip() == "":
        p = default_weights_path()
    else:
        p = Path(path)
        if not p.is_absolute():
            from scripts.utils.data_path import data_path
            p = data_path("data-config") / p
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuleUnavailable(f"no weights file at {p}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuleUnavailable(f"weights file {p} unreadable: {exc}") from exc
    if not isinstance(doc, dict):
        raise RuleUnavailable(f"weights file {p} is not a JSON object")
    return RuleModel(doc, p)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sentinel fallback rule: self-check over a weights file")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="load the weights and print what the runtime covers (counts only)")
    c.add_argument("--weights", default=None)
    a = ap.parse_args()
    try:
        rule = load_rule(a.weights)
    except RuleUnavailable as exc:
        print(f"rule unavailable: {exc}", file=sys.stderr)
        return 1
    used = [n for n, (w, *_r) in rule.features.items() if w != 0]
    print(f"weights: {rule.source}")
    print(f"fitted_at: {rule.fitted_at}")
    print(f"features with weight: {len(used)}; computed at runtime: {len(used) - len(rule.missing)}; "
          f"imputed at training mean: {len(rule.missing)} ({', '.join(rule.missing) or 'none'})")
    print(f"runtime block: own_domains={len(rule.own_domains)} name_caps={len(rule.name_caps)} "
          f"weekend_days={rule.weekend_days or 'unset'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
