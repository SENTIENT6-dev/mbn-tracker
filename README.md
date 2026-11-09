# IO PM Tracker

Replacement for `MBN.accdb`: projects, PCCF budgets, POs, invoices, change orders and MPSR status,
as a local web app (Python + Django + SQLite). Data lives in `db.sqlite3` in this folder.

**Each project manager has their own tracker.** The first time it opens, it asks who you are (you become
the PM on every project in it), then offers to import your projects from the Access database.

## Start it
Double-click `start.bat`. It opens http://127.0.0.1:8000/ in your browser.

## Try it with demo data
`manage.py seed_demo` fills an empty database with made-up projects, POs, invoices and buildings
(all names and numbers are fictional). `manage.py seed_demo --reset` wipes the database first.

## Hosted demo (Render)
The repo includes `render.yaml`. On [render.com](https://render.com): **New → Blueprint**, pick this repository,
and enter a value for `MBN_PASSWORD` when asked. Render builds it, loads the demo data and gives you a link.
Everyone who opens the link must enter that password (change it under the service's *Environment* tab).
The free plan's disk is temporary: the demo data is reloaded whenever the service restarts, so don't keep real work there.

Settings for any server (environment variables):
| Variable | Purpose |
|---|---|
| `MBN_PASSWORD` | Password to open the site. Blank = no password (fine on your own PC) |
| `MBN_DEBUG` | `0` on a server |
| `MBN_SECRET_KEY` | Long random string, required when `MBN_DEBUG=0` |
| `MBN_HOSTS` | The site's host name(s), comma-separated |
| `MBN_DB` | Path of the SQLite database file |
| `MBN_BACKUP_DIR` | Folder for backups (default: `backups` next to the tracker) |
| `MBN_BACKUP_KEEP_DAYS` | How long daily backups are kept (default 30) |
| `MBN_BACKUP_DAILY` | `0` turns the automatic daily backup off |

## Giving a copy to another PM
1. Copy this folder **without** `db.sqlite3`, `db.backup-*.sqlite3` and `.venv`.
2. On their PC (Python 3.9+ installed): double-click `install.bat` once.
3. Double-click `start.bat`. The tracker asks for their name and details, then for the Access file.
   With *Only projects where I'm the project manager* ticked, a shared team Access file gives them just their
   projects. (Records of projects that were deleted in Access have no PM, so they're left out in that mode.)

Later reloads from a newer Access file are under **Settings** (click your name, bottom left). Your details
and the PM filter are kept; everything else is replaced by what's in the Access file.

From the command line: `manage.py import_access --reset [--source PATH] [--pm "First Last" | --all-pms]`.
Set `MBN_DB` to another file path to try things on a copy of the database.

## Screens
| Screen | What it shows |
|---|---|
| Overview | Headline totals, budget use per active project, what needs attention, upcoming milestones, projects by stage |
| MPSR | The monthly pass (was the MPSR form): go through each active project, review budget, cash flow and milestones, update status comment, risk, stage/actual dates, forecast dates, forecast cash flow and value of work. *Save & next* moves on; each project is marked done for the month, and the Overview shows progress |
| Calendar | Month view of every milestone (design end, award, substantial, closeout), with overdue and next-120-days lists |
| Projects | All projects with budget bars, health and next milestone (was Project_Financials) |
| Project page | Stage tracker, budget bar, milestone timeline, cash flow chart, VOW, comments/risk, PCCFs, POs, history (was MPSR_Form, Projects_Form) |
| Purchase orders / PO page | PO_Form, New_PO, Add_New_Invoice, All_Invoices_By_PO |
| Invoices | Every invoice with search, fiscal-year and date filters, monthly chart, totals by vendor; add (pick the PO), edit, delete |
| Change orders | Pending_changes form |
| Vendors | Every company with POs, committed/invoiced totals, invoicing per year, contacts; add, edit, merge, delete |
| Facilities | Buildings (8,500+) and properties tables with search, building contacts (FM, PREM…) and projects per building |
| Data issues | Problems found while importing from Access, and how each was resolved |

### Invoice limits
When adding an invoice (Invoices tab or a PO page) the form shows, for the chosen PO:
- **PO remaining**: PO value + approved change orders − already invoiced on the PO.
- **Project budget left after fees**: latest PCCF budget excluding the fee − already invoiced on the project
  (skipped when the project has no PCCF).
- **Available for this invoice**: the smaller of the two.

If the invoice amount is more than what's available, Save is greyed out with an explanation of which approval
is needed (a change order for the PO, or a revised PCCF for the project). The server applies the same rule,
including when editing an invoice. Credits (negative amounts) are always allowed.

### Editing and deleting
Every record has Edit and Delete. Related figures update immediately because totals are always computed live.
- **Delete project** removes its PCCFs, POs, invoices, change orders, VOW and history (type the project number to confirm).
  To keep a record but hide it, set the status to Closed or Cancelled instead.
- **Delete PO** removes its invoices and change orders. Changing a PO's project moves its invoices and change orders too.
- **Change orders**: editing or deleting one that was already processed adjusts the PO value by the difference.
- **Vendors** with POs or client projects can't be deleted; use *Merge into another company* to move everything first.
- **Buildings**: deleting one keeps the asset number on its projects as text; adding a building with that number re-links them.

Hover over any bar, chart or calendar entry for its details.

### Charter (PCCF 0)
- **New project** is a single page: project details plus its charter (PCCF submission 0), saved together.
  Required: IO project #, title, status, kind, **building**, and a charter budget above $0.
- The building must be in the buildings list. Typing suggests matches by asset #, name or address; if it isn't
  listed, an *Add building* link opens the building form (pre-filled) in a new tab. Add it there, come back, and save.
  Older projects whose building was never listed can still be edited without changing it.
  The charter needs a budget; the fee fills in from the fee % and the total updates as you type.
  The MPSR forecast (cash flow and milestone dates) starts from the charter's approved values.
- **Cash flow must equal the total budget.** On the charter and every PCCF (new or edited), previous years +
  Q1–Q4 + next year + future must add up to the total budget including fee (within $1 for rounding). A bar above
  Save shows both totals; Save stays greyed out with an explanation until they match. All PCCFs imported from
  Access already balance.
- PCCF and charter forms are laid out in sections: submission, budget (design / construction / fee), approved
  schedule (six milestones in order), approved cash flow (previous years · Q1–Q4 with a this-year total · next
  and future years), notes. Confirmed drawings, revised occupancy, estimate class and stream were removed.
- **Approved schedule dates must be in order**: charter approval ≤ design start ≤ design end ≤ construction
  award ≤ substantial performance ≤ closeout (blank dates are skipped). Dates out of order turn red and Save is
  blocked. Projects whose *current* PCCF from Access breaks this are listed on Data issues; the flag clears
  when the PCCF is saved with corrected dates.
- **Fee amount is calculated, not typed**: (design + construction + both contingencies) × fee %, to the cent.
  It's shown read-only and updates as you type; the server recalculates it on save.
- **Amounts are shown in financial format** (1,234,567.89) in every form. You can type `1234567.89`,
  `$1,234,567.89` or `(1,000)` for a negative; it's tidied up when you leave the box.
- PCCF submission numbers are generated, not typed: the charter is 0 and each new PCCF gets the next number.
  Numbers never change once assigned.
- The charter can be edited but not deleted or renumbered, and a project's last remaining PCCF can't be deleted.
  To remove a project entirely, delete the project.
- Older projects imported without a charter show a note with an *Add the charter* link.

### PCCF notes and fiscal years
- Each PCCF has one **Notes** field. The four Access fields (scope, methodology, milestone and budget changes)
  were merged into it: "No change" answers dropped, and sentences repeated between them kept only once.
  Notes come up for each submission on a project's *Budget & PCCFs* tab.
- Fiscal years run April to March and are shown as **2024/25**. They're stored as the year the fiscal year ends
  (2025), which is how Access stored them.

### Milestone marks
The calendar shows every project's milestones, past and future (switch to active projects only with the drop-down).
- **Green ✓**: an actual date is recorded.
- **Red ✗**: the date has passed and no actual date is recorded.
- **●**: upcoming (forecast date in the future).

"Overdue" counts on the Overview, project health labels and the calendar's side list only include red ✗
milestones on active and on-hold projects, since those are the ones that need action.

The calendar uses the MPSR forecast date, or the approved PCCF date when there's no forecast.

## Rules the database now enforces
- One project per IO number; one PCCF per project + submission number; one VOW entry per project per day.
- POs must belong to an existing project and vendor; invoices to an existing PO. Records that are in use can't be deleted.
- Change orders take their project from their PO. Only **approved** change orders count as pending.
  Processing one adds it to the PO value, and can only happen once.
- Adding an invoice warns if the number is already on that PO, or if it exceeds what's left on the PO.

## Re-importing from Access
```
.venv\Scripts\python.exe manage.py import_access --reset
.venv\Scripts\python.exe manage.py reconcile_access
```
`import_access` reads a temporary copy of `MBN.accdb` (default `Downloads\MBN.accdb` in your user folder, or set
`MBN_ACCESS_SOURCE`), so the original is never touched. Use `--source <path>` for a different file.
`--reset` **replaces everything** in this app, including any work done here since the last import.
`reconcile_access` compares each project's totals with Access's `Project_Financials` query.
The import finishes by running `resolve_issues`, which fixes the known Access data problems
(e.g. mistyped invoice dates, POs left on deleted projects) and records the reasoning on the Data issues page.

## Tests
```
.venv\Scripts\python.exe manage.py test tracker
```

## Backups
The tracker saves a copy of the database automatically the first time it's used each day, into the `backups`
folder (not sent to GitHub). Daily copies are kept for 30 days. **Settings → Back up now** makes an extra copy,
and one is also made before a fiscal-year rollover; those are kept until you delete them.

To restore: close the tracker, rename `db.sqlite3` (to keep it), copy the backup in its place as `db.sqlite3`,
and start the tracker again.

To keep backups somewhere else, such as a OneDrive folder, set `MBN_BACKUP_DIR` (see the table above).
