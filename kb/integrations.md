# Integrations

## HubSpot
Connect HubSpot from Settings, Integrations, HubSpot. Contacts sync every 15 minutes. Custom properties must be mapped once in the field mapping screen.

## Google Sheets
The Google Sheets export runs hourly and appends new rows to the selected sheet. The sheet must be shared with the integration's service account email.

## Zapier and n8n
Use the API key from Settings, API. Webhooks are retried 5 times over 30 minutes if your endpoint returns an error.
