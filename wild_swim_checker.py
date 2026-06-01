#!/usr/bin/env python3
"""Water quality email checker for Port Meadow wild swims."""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback if ever needed.
    ZoneInfo = None


ARCGIS_QUERY_URL = (
    "https://services2.arcgis.com/g6o32ZDQ33GpCIu3/arcgis/rest/services/"
    "STEServiceProduction/FeatureServer/0/query"
)
EDM_MAP_URL = "https://www.thameswater.co.uk/edm-map"
LONDON_TZ = ZoneInfo("Europe/London") if ZoneInfo else timezone.utc

CRITICAL_SITES = ("Witney", "Cassington")
UPSTREAM_SITES = (
    "Church Hanborough",
    "South Leigh",
    "Standlake",
    "Stanton Harcourt",
    "Combe",
    "Woodstock",
)
MONITORED_SITES = CRITICAL_SITES + UPSTREAM_SITES

FIELDS = (
    "LocationName",
    "AlertStatus",
    "AlertPast48Hours",
    "MostRecentDischargeAlertStart",
    "MostRecentDischargeAlertStop",
    "ReceivingWaterCourse",
)

SCHEDULE = {
    "monday": {
        "swim_day": 0,
        "swim_time": "08:00",
        "location": "Port Meadow",
        "checks": {"evening": (6, "19:00"), "morning": (0, "06:30")},
    },
    "thursday": {
        "swim_day": 3,
        "swim_time": "08:00",
        "location": "Port Meadow",
        "checks": {"evening": (2, "19:00"), "morning": (3, "06:30")},
    },
    "saturday": {
        "swim_day": 5,
        "swim_time": "11:00",
        "location": "Port Meadow",
        "checks": {"evening": (4, "19:00"), "morning": (5, "06:30")},
    },
}


@dataclass(frozen=True)
class SiteStatus:
    name: str
    alert_status: str
    alert_past_48_hours: bool
    start: Optional[datetime]
    stop: Optional[datetime]
    receiving_water_course: str

    def is_currently_discharging(self) -> bool:
        return normalize_status(self.alert_status) == "discharging"

    def has_recent_discharge(self) -> bool:
        return self.is_currently_discharging() or self.alert_past_48_hours

    def duration(self, now: datetime) -> Optional[timedelta]:
        if not self.start:
            return None
        end = self.stop or now if self.is_currently_discharging() else self.stop
        if end is None:
            return None
        return end - self.start


@dataclass(frozen=True)
class SwimRun:
    swim_key: str
    check_type: str
    swim_datetime: datetime
    location: str


@dataclass(frozen=True)
class Decision:
    status: str
    recommendation: str
    reasons: Tuple[str, ...]
    warnings: Tuple[str, ...] = ()


class ManualCheckRequired(Exception):
    pass


def normalize_status(value: str) -> str:
    return (value or "").strip().lower()


def parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    raise ManualCheckRequired(f"Unexpected AlertPast48Hours value: {value!r}")


def parse_arcgis_time(value: object, field_name: str, site_name: str) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise ManualCheckRequired(f"{site_name} has malformed {field_name}: {value!r}")
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).astimezone(LONDON_TZ)


def format_duration(duration: Optional[timedelta]) -> str:
    if duration is None:
        return "-"
    total_minutes = max(0, int(duration.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def format_time(value: Optional[datetime]) -> str:
    if value is None:
        return "-"
    return value.astimezone(LONDON_TZ).strftime("%a %d %b %H:%M")


def query_arcgis() -> List[dict]:
    quoted_sites = ", ".join(f"'{site.replace(chr(39), chr(39) + chr(39))}'" for site in MONITORED_SITES)
    params = {
        "f": "json",
        "where": f"LocationName IN ({quoted_sites})",
        "outFields": ",".join(FIELDS),
        "returnGeometry": "false",
    }
    url = f"{ARCGIS_QUERY_URL}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "wild-swim-checker/1.0"})
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if "error" in payload:
        raise ManualCheckRequired(f"ArcGIS returned an error: {payload['error']}")
    return payload.get("features", [])


def parse_sites(features: Iterable[dict]) -> Dict[str, SiteStatus]:
    sites: Dict[str, SiteStatus] = {}
    for feature in features:
        attrs = feature.get("attributes") or {}
        name = attrs.get("LocationName")
        if name not in MONITORED_SITES:
            continue
        start = parse_arcgis_time(attrs.get("MostRecentDischargeAlertStart"), "MostRecentDischargeAlertStart", name)
        stop = parse_arcgis_time(attrs.get("MostRecentDischargeAlertStop"), "MostRecentDischargeAlertStop", name)
        if start and stop and stop < start:
            raise ManualCheckRequired(f"{name} has a stop time before its start time.")
        alert_status = attrs.get("AlertStatus") or ""
        normalized = normalize_status(alert_status)
        if normalized not in {"not discharging", "discharging", "offline", ""}:
            raise ManualCheckRequired(f"{name} has unexpected AlertStatus: {alert_status!r}")
        sites[name] = SiteStatus(
            name=name,
            alert_status=alert_status or "Unknown",
            alert_past_48_hours=parse_bool(attrs.get("AlertPast48Hours")),
            start=start,
            stop=stop,
            receiving_water_course=attrs.get("ReceivingWaterCourse") or "-",
        )
    missing = [site for site in MONITORED_SITES if site not in sites]
    if missing:
        raise ManualCheckRequired(f"Missing monitored site(s): {', '.join(missing)}")
    return sites


def evaluate(sites: Dict[str, SiteStatus], now: datetime) -> Decision:
    reasons: List[str] = []
    warnings: List[str] = []

    for site_name in CRITICAL_SITES:
        site = sites[site_name]
        if site.has_recent_discharge():
            reasons.append(f"{site.name} has discharged in the last 48 hours or is discharging now.")

    upstream_recent = [sites[name] for name in UPSTREAM_SITES if sites[name].has_recent_discharge()]
    if len(upstream_recent) >= 2:
        names = ", ".join(site.name for site in upstream_recent)
        reasons.append(f"Two or more upstream sites have discharged in the last 48 hours: {names}.")

    for site in upstream_recent:
        duration = site.duration(now)
        if duration is not None and duration >= timedelta(hours=4):
            reasons.append(f"{site.name} discharge duration is {format_duration(duration)}, at or above 4 hours.")

    for site in sites.values():
        if normalize_status(site.alert_status) in {"offline", ""}:
            warnings.append(f"{site.name} monitor is offline/unknown; verify manually if concerned.")

    if reasons:
        return Decision(
            status="CANCEL",
            recommendation="Cancel / do not swim at Port Meadow. Consider Hinksey only after a human check.",
            reasons=tuple(reasons),
            warnings=tuple(warnings),
        )

    return Decision(
        status="OK",
        recommendation="OK for Port Meadow under the current society EDM rules.",
        reasons=("No monitored site triggers the current cancellation rules.",),
        warnings=tuple(warnings),
    )


def manual_decision(reason: str) -> Decision:
    return Decision(
        status="MANUAL CHECK",
        recommendation="Manual check required before deciding whether to swim.",
        reasons=(reason, f"Check the Thames Water EDM map: {EDM_MAP_URL}"),
    )


def make_swim_run(swim_key: str, check_type: str, now: Optional[datetime] = None) -> SwimRun:
    now = (now or datetime.now(LONDON_TZ)).astimezone(LONDON_TZ)
    if swim_key not in SCHEDULE:
        raise ValueError(f"Unknown swim: {swim_key}")
    if check_type not in {"evening", "morning"}:
        raise ValueError(f"Unknown check type: {check_type}")
    config = SCHEDULE[swim_key]
    days_until_swim = (config["swim_day"] - now.weekday()) % 7
    swim_date = (now + timedelta(days=days_until_swim)).date()
    hour, minute = (int(part) for part in config["swim_time"].split(":"))
    swim_datetime = datetime(swim_date.year, swim_date.month, swim_date.day, hour, minute, tzinfo=LONDON_TZ)
    return SwimRun(swim_key=swim_key, check_type=check_type, swim_datetime=swim_datetime, location=config["location"])


def due_runs(now: Optional[datetime] = None) -> List[SwimRun]:
    now = (now or datetime.now(LONDON_TZ)).astimezone(LONDON_TZ)
    runs: List[SwimRun] = []
    for swim_key, config in SCHEDULE.items():
        for check_type, (weekday, check_time) in config["checks"].items():
            hour, minute = (int(part) for part in check_time.split(":"))
            scheduled_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            in_check_window = scheduled_at - timedelta(minutes=15) <= now < scheduled_at + timedelta(minutes=15)
            if now.weekday() == weekday and in_check_window:
                runs.append(make_swim_run(swim_key, check_type, now))
    return runs


def build_email_body(run: SwimRun, decision: Decision, sites: Optional[Dict[str, SiteStatus]], checked_at: datetime) -> str:
    lines = [
        f"Swim: {run.swim_datetime.strftime('%A %d %B %Y %H:%M')} at {run.location}",
        f"Check: {run.check_type} check at {checked_at.astimezone(LONDON_TZ).strftime('%A %d %B %Y %H:%M %Z')}",
        "",
        f"Recommendation: {decision.recommendation}",
        "",
        "Reasons:",
    ]
    lines.extend(f"- {reason}" for reason in decision.reasons)
    if decision.warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in decision.warnings)
    if run.check_type == "morning":
        lines.extend(["", "Weather reminder: check for weather warnings or named storms before the swim."])
    else:
        lines.extend(["", "Weather reminder: if rain is forecast, the morning EDM check is especially important."])

    lines.extend(["", "Monitored sites:"])
    if sites:
        for name in MONITORED_SITES:
            site = sites[name]
            duration = site.duration(checked_at)
            lines.append(
                "- "
                f"{site.name}: {site.alert_status}; "
                f"past 48h={str(site.alert_past_48_hours).lower()}; "
                f"started={format_time(site.start)}; stopped={format_time(site.stop)}; "
                f"duration={format_duration(duration)}; feeds into {site.receiving_water_course}"
            )
    else:
        lines.append("- Site data unavailable; use the EDM map manually.")
    lines.extend(["", f"Thames Water EDM map: {EDM_MAP_URL}"])
    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST")
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("EMAIL_FROM")
    recipients = [item.strip() for item in os.environ.get("EMAIL_TO", "").split(",") if item.strip()]
    port = int(os.environ.get("SMTP_PORT", "587"))
    use_tls = os.environ.get("SMTP_USE_TLS", "true").strip().lower() != "false"

    missing = [
        name
        for name, value in {
            "SMTP_HOST": host,
            "SMTP_USERNAME": username,
            "SMTP_PASSWORD": password,
            "EMAIL_FROM": sender,
            "EMAIL_TO": recipients,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing email environment variable(s): {', '.join(missing)}")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        if use_tls:
            smtp.starttls(context=ssl.create_default_context())
        smtp.login(username, password)
        smtp.send_message(message)


def run_check(run: SwimRun, print_only: bool = False, now: Optional[datetime] = None) -> Decision:
    checked_at = (now or datetime.now(LONDON_TZ)).astimezone(LONDON_TZ)
    sites: Optional[Dict[str, SiteStatus]] = None
    try:
        sites = parse_sites(query_arcgis())
        decision = evaluate(sites, checked_at)
    except Exception as exc:
        decision = manual_decision(str(exc))
    subject = f"Wild swim check: {decision.status}"
    body = build_email_body(run, decision, sites, checked_at)
    if print_only:
        print(f"Subject: {subject}\n\n{body}")
    else:
        send_email(subject, body)
    return decision


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Port Meadow wild swim water quality and email the result.")
    parser.add_argument("--swim", choices=sorted(SCHEDULE), help="Swim day to check.")
    parser.add_argument("--check", choices=("evening", "morning"), help="Check type.")
    parser.add_argument("--run-due", action="store_true", help="Run checks due at the current Europe/London time.")
    parser.add_argument("--print-only", action="store_true", help="Print the email instead of sending it.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.run_due:
        runs = due_runs()
        if not runs:
            print("No swim checks due at this time.")
            return 0
    else:
        if not args.swim or not args.check:
            print("Either use --run-due or provide both --swim and --check.", file=sys.stderr)
            return 2
        runs = [make_swim_run(args.swim, args.check)]

    exit_code = 0
    for run in runs:
        decision = run_check(run, print_only=args.print_only)
        if decision.status == "MANUAL CHECK":
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
