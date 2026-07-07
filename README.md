# Newbotix — KEENON Elevator Webhook Server

שרת webhook שמגשר בין רובוטי KEENON למעליות Schindler.

## הגדרת משתני סביבה ב-Railway

| משתנה | ערך |
|-------|-----|
| SCHINDLER_KEY | Subscription Key מעוז |
| PORT | 5000 |

## Endpoints

- `GET /` — בדיקת תקינות
- `GET /status` — סטטוס חיבורים
- `POST /elevator-callback` — webhook מ-KEENON
