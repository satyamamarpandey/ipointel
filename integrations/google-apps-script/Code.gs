/**
 * IPO Intelligence - GitHub Pages waitlist endpoint (Google Apps Script Web App).
 *
 * This is the ONLY thing that stands in for a real backend on the static
 * (GitHub Pages) deployment: FastAPI/PostgreSQL cannot run there, so a
 * signup on ipointel.brandsap.com posts here instead, and this script writes
 * it straight into a Google Sheet. No credentials of any kind are embedded
 * in the frontend - Apps Script Web Apps run under the deploying Google
 * account's own authorization, entirely server-side.
 *
 * Deploy (manual, one-time - Apps Script has no CLI-deployable path from
 * this environment): open script.google.com, paste this file into a new
 * project bound to (or referencing) the target Sheet, Deploy > New deployment
 * > type "Web app", execute as "Me", access "Anyone". Copy the resulting
 * /exec URL into the PUBLIC_WAITLIST_ENDPOINT repository secret used by
 * .github/workflows/pages.yml (see docs/GITHUB_PAGES.md).
 *
 * Sheet columns (row 1 header, created automatically if missing):
 * Timestamp | Email | Name | Markets | Source | Page | Referral
 */

var SHEET_NAME = 'Waitlist';
var HEADER = ['Timestamp', 'Email', 'Name', 'Markets', 'Source', 'Page', 'Referral'];

function doPost(e) {
  try {
    var payload = JSON.parse(e.postData.contents || '{}');
    var email = String(payload.email || '').trim().toLowerCase();
    if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      return respond({ ok: false, error: 'A valid email is required.' }, 400);
    }
    var name = String(payload.name || '').trim().slice(0, 160);
    var markets = ['india', 'us', 'both'].indexOf(payload.markets) >= 0 ? payload.markets : 'both';
    var source = String(payload.source || 'pages').trim().slice(0, 80);
    var page = String(payload.page || '').trim().slice(0, 160);

    var sheet = getSheet();
    var existingRow = findRowByEmail(sheet, email);
    if (existingRow) {
      return respond({ ok: true, message: "You're already on the early-access list." });
    }
    sheet.appendRow([new Date().toISOString(), email, name, markets, source, page, '']);
    return respond({ ok: true, message: 'Early access reserved. We will notify you about launch and material IPO-score changes.' });
  } catch (err) {
    return respond({ ok: false, error: 'Could not process signup: ' + err.message }, 500);
  }
}

function getSheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
    sheet.appendRow(HEADER);
  }
  return sheet;
}

function findRowByEmail(sheet, email) {
  var values = sheet.getDataRange().getValues();
  for (var i = 1; i < values.length; i++) {
    if (String(values[i][1]).trim().toLowerCase() === email) return i + 1;
  }
  return 0;
}

function respond(obj, status) {
  // Apps Script Web Apps cannot set a custom HTTP status code on the
  // response; failures are signaled via {ok:false} in the body instead,
  // which app/static/pages-adapter.js already checks for.
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
