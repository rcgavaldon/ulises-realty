"""One-time: create the 'Ulises Realty Demo' calendar owned by the Sofia booking
service account, share it read/write with Robert, print the calendar id."""
from google.oauth2 import service_account
from googleapiclient.discovery import build

SA = r"C:\Users\rober\Downloads\sofia-calendar-498722-137a2d541c24.json"
creds = service_account.Credentials.from_service_account_file(
    SA, scopes=["https://www.googleapis.com/auth/calendar"])
svc = build("calendar", "v3", credentials=creds)

# reuse if it already exists
for item in svc.calendarList().list().execute().get("items", []):
    if item.get("summary") == "Ulises Realty Demo":
        print("CAL_ID=" + item["id"])
        raise SystemExit

cal = svc.calendars().insert(body={
    "summary": "Ulises Realty Demo",
    "timeZone": "America/Denver",
}).execute()
svc.acl().insert(calendarId=cal["id"], body={
    "role": "writer",
    "scope": {"type": "user", "value": "roberto.gavaldon3@gmail.com"},
}).execute()
print("CAL_ID=" + cal["id"])
