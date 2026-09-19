# Northwind (SQLite3) — vendored dataset

Source: <https://github.com/jpwhite3/northwind-SQLite3> (MIT, © 2016 JP White — see `LICENSE`).
The classic Microsoft Northwind trading company: customers, orders, order lines, products,
categories, suppliers, employees, sales territories/regions and shippers.

| File | What it is |
|---|---|
| `northwind.db` | Upstream `dist/northwind.db`, untouched (byte-identical): 13 tables, 16 upstream views, ~16k orders / ~609k order lines spanning 2012-07 → 2023-10 |
| `schema.sql` | The DDL of upstream `src/create.sql` (INSERT statements removed) — the schema the `.db` was built with |

`app.db.seed.analytics_seed` copies `northwind.db` to `ANALYTICS_SQLITE_PATH` and creates Lens's own
`v_*` views from `analytics_catalog.py` on top. Upstream files are never edited; the upstream views stay
in the file but are not on Lens's allow-list.

To swap datasets: replace the two files here, rewrite `analytics_catalog.py` (views, scope key,
dataset notes) and reseed. Nothing else needs to know the domain.
