# Inventory Management - requirements

## R1. Inventory items (implemented)
Users manage inventory items with SKU, name, quantity and price through a web UI and a JSON API.
- Create, list, edit and delete items; SKUs are unique.
- Search by name or SKU; pagination with 10 items per page.
- Validation errors are shown next to the form fields.

## R2. Customer management (NOT YET IMPLEMENTED)
Users manage customers who buy inventory items.
- A customer has a name (required), a unique e-mail address (required, valid format) and an optional phone number.
- JSON API: `GET/POST /api/customers`, `GET/PUT/PATCH/DELETE /api/customers/<id>` with the same status-code
  conventions as the items API (201 on create, 400 validation, 404 unknown, 409 duplicate e-mail, 415 non-JSON).
- Web UI: a "Customers" page listing customers, plus forms to create and edit customers; invalid e-mail
  addresses must be rejected with a visible error message.
- Customers are stored in the application's SQLite database.
