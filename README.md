# Wild Swim Water Quality Email Alerts

Scheduled email checker for Port Meadow wild swims. It queries the Thames Water
EDM ArcGIS layer, applies the society rules, and emails a recommendation for
each fixed weekly swim check.

## What It Checks

Swims:

- Monday 08:00, Port Meadow: Sunday 19:00 and Monday 06:30 checks.
- Thursday 08:00, Port Meadow: Wednesday 19:00 and Thursday 06:30 checks.
- Saturday 11:00, Port Meadow: Friday 19:00 and Saturday 06:30 checks.

Monitored Thames Water sites:

- Automatic cancellation sites: `Witney`, `Cassington`.
- Upstream group: `Church Hanborough`, `South Leigh`, `Standlake`,
  `Stanton Harcourt`, `Combe`, `Woodstock`.

The checker returns:

- `OK` when no rule triggers.
- `CANCEL` when the policy says not to swim at Port Meadow.
- `MANUAL CHECK` when the ArcGIS data cannot be fetched or trusted.

## Run Locally

Print an email preview without sending:

```sh
python3 wild_swim_checker.py --swim monday --check morning --print-only
```

Run whatever check is due at the current Europe/London time:

```sh
python3 wild_swim_checker.py --run-due --print-only
```

## Email Configuration

Set these environment variables before sending real email:

```sh
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USERNAME="your-email@example.com"
export SMTP_PASSWORD="your-app-password"
export EMAIL_FROM="your-email@example.com"
export EMAIL_TO="your-email@example.com"
```

`SMTP_USE_TLS` defaults to `true`. Set it to `false` only if your SMTP provider
requires that.

## GitHub Actions Setup

The workflow in `.github/workflows/wild-swim-check.yml` runs every 30 minutes
and lets the Python script decide whether a London-time swim check is due. This
avoids hard-coding UTC offsets and breaking during daylight saving changes.

Add these repository secrets:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `EMAIL_FROM`
- `EMAIL_TO`

The workflow can also be run manually from the GitHub Actions tab.

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests cover the decision rules and schedule matching. A live manual check
can be done with:

```sh
python3 wild_swim_checker.py --swim saturday --check morning --print-only
```
