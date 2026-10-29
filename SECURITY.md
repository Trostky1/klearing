# Security

Klearing keeps your data on your own machine. It has no hosted service.

## Report a problem

Open a private security advisory on GitHub:
**Security tab > Report a vulnerability**.

Please do not post secrets, bank tokens, or real financial data in a public issue.

## What to know

- Credentials sit encrypted in a local SQLite database.
- The encryption key lives in your operating system credential store.
- The app listens on `127.0.0.1` only.
- Use sandbox keys (Plaid sandbox, test Shopify store) when you try the app.
