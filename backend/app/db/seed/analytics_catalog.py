"""The analytics catalogue: everything Lens knows about the dataset lives in this one file.

* ``VIEWS`` — one definition per exposed view.  The analytics seed creates them (SQLite SQL)
  on top of the vendored Northwind schema; the app seed registers ``description``,
  ``business_rules``, ``columns`` and ``scope_column`` into ``data_source_view`` (the
  allow-list the model sees).
* ``SCOPE_KEY`` — the row-level scope column.  A principal is granted the sales *regions*
  they may see (``region_id``: 1 Eastern, 2 Western, 3 Northern, 4 Southern); every view
  that carries order rows exposes ``region_id`` — the region of the employee who took the
  order — so the scope predicate has a column to bind to.  Reference views (products,
  customers, suppliers) are region-agnostic and unscoped.
* ``DATASET`` — the domain wording the prompts, the seeded demo users and the UI take from
  here instead of hard-coding it.

To move Lens to a different dataset: drop the new ``.db`` + ``schema.sql`` into a vendored
folder, rewrite this file, reseed.  Tests that pin expected numbers still need updating.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str
    description: str
    sensitive: bool = False

    def as_dict(self) -> dict:
        return {"name": self.name, "type": self.type, "description": self.description, "sensitive": self.sensitive}


@dataclass(frozen=True, slots=True)
class ViewDef:
    name: str
    description: str
    sql: str
    columns: list[Column]
    business_rules: str | None = None
    scope_column: str | None = "region_id"
    allow_row_samples: bool = False
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DatasetInfo:
    name: str  # data_source.name
    domain: str  # one sentence for the agent prompt: what the company does and what the data covers
    topics: str  # comma list of legitimate subjects, for the injection screen's allow rule
    group_examples: str  # example dashboard section titles, for the grouping prompt
    headline: str  # UI: "Ask a question about …"
    suggested_questions: list[str]
    scope_label: str  # UI / docs: what a scope value means
    # demo principals: scope values for the two restricted seed users (the other two are unrestricted)
    demo_scope_partial: list[int]
    demo_scope_single: list[int]


SCOPE_KEY = "region_id"

DATASET = DatasetInfo(
    name="Northwind Traders",
    domain=(
        "a specialty-food trading company (Northwind Traders): customer orders and order lines, products by "
        "category and supplier, sales employees organised in four sales regions, shippers and freight, "
        "from July 2012 to October 2023"
    ),
    topics=(
        "orders, order lines, revenue, discounts, freight, customers and countries, products, categories, "
        "suppliers, stock levels and reorder points, sales employees and regions, shippers and delivery times, "
        "comparisons, trends, rankings"
    ),
    group_examples='"Revenue & trends", "Customers & countries", "Products & categories", "Shipping performance"',
    headline="Ask a question about Northwind's sales",
    suggested_questions=[
        "How has monthly revenue trended over the last three years?",
        "Which ten customers generate the most revenue, and from which countries?",
        "Which product categories sell best in each region?",
        "Which shipper delivers fastest, and how often are orders shipped after the required date?",
    ],
    scope_label="sales region (1 Eastern, 2 Western, 3 Northern, 4 Southern)",
    demo_scope_partial=[1, 2],
    demo_scope_single=[1],
)

# Views are written fact-table-first with CROSS JOIN, which SQLite honours as a join-order hint: left to
# itself the planner starts the 609k-row order-lines views from Categories/Employees and takes ~8 s
# instead of <1 s.  CROSS JOIN ... ON is an ordinary inner join otherwise.

# the region an employee sells for: every employee's territories lie in exactly one region
_EMPLOYEE_REGION = """
        (SELECT et.EmployeeID, MIN(t.RegionID) AS RegionID
         FROM EmployeeTerritories et
         JOIN Territories t ON t.TerritoryID = et.TerritoryID
         GROUP BY et.EmployeeID)"""

_REGION_COLS = [
    Column("region_id", "integer", "Sales region identifier (row-level scope column)"),
    Column("region_name", "text", "Sales region: Eastern, Western, Northern or Southern"),
]
_EMPLOYEE_COLS = [
    Column("employee_id", "integer", "Sales employee identifier"),
    Column("employee_name", "text", "Sales employee (first and last name)"),
]
_CUSTOMER_COLS = [
    Column("customer_id", "text", "Customer identifier (5-letter code)"),
    Column("customer_name", "text", "Customer company name"),
    Column("customer_country", "text", "Customer's country"),
]
_PRODUCT_COLS = [
    Column("product_id", "integer", "Product identifier"),
    Column("product_name", "text", "Product name"),
    Column("category_name", "text", "Product category (Beverages, Condiments, Confections, Dairy Products, …)"),
]

VIEWS: list[ViewDef] = [
    ViewDef(
        name="v_orders",
        description="One row per customer order with its dates, customer, salesperson, region, shipper, freight and totals.",
        business_rules=(
            "order_total = SUM(unit_price * quantity * (1 - discount)) over the lines: this is 'revenue' / 'sales'. "
            "Freight is charged separately and is not part of order_total. is_shipped = 1 when shipped_date is set; "
            "shipped_late = 1 when shipped_date > required_date ('late', 'overdue', 'missed the required date'). "
            "days_to_ship = shipped_date - order_date in days. order_month is YYYY-MM and order_year YYYY (text) for "
            "grouping. region is the sales region of the employee who took the order."
        ),
        sql=f"""
        SELECT o.OrderID AS order_id,
               DATE(o.OrderDate) AS order_date,
               strftime('%Y-%m', o.OrderDate) AS order_month,
               strftime('%Y', o.OrderDate) AS order_year,
               DATE(o.RequiredDate) AS required_date,
               DATE(o.ShippedDate) AS shipped_date,
               CASE WHEN o.ShippedDate IS NOT NULL THEN 1 ELSE 0 END AS is_shipped,
               CASE WHEN o.ShippedDate IS NOT NULL
                    THEN CAST(julianday(DATE(o.ShippedDate)) - julianday(DATE(o.OrderDate)) AS INTEGER) END AS days_to_ship,
               CASE WHEN o.ShippedDate IS NOT NULL AND DATE(o.ShippedDate) > DATE(o.RequiredDate) THEN 1 ELSE 0 END
                   AS shipped_late,
               c.CustomerID AS customer_id, c.CompanyName AS customer_name, c.Country AS customer_country,
               e.EmployeeID AS employee_id, e.FirstName || ' ' || e.LastName AS employee_name,
               r.RegionID AS region_id, r.RegionDescription AS region_name,
               sh.CompanyName AS shipper_name,
               o.ShipCity AS ship_city, o.ShipCountry AS ship_country,
               ROUND(o.Freight, 2) AS freight,
               COUNT(od.ProductID) AS line_count,
               COALESCE(SUM(od.Quantity), 0) AS total_units,
               ROUND(COALESCE(SUM(od.UnitPrice * od.Quantity * (1 - od.Discount)), 0), 2) AS order_total
        FROM Orders o
        CROSS JOIN Customers c ON c.CustomerID = o.CustomerID
        CROSS JOIN Employees e ON e.EmployeeID = o.EmployeeID
        CROSS JOIN {_EMPLOYEE_REGION} er ON er.EmployeeID = e.EmployeeID
        CROSS JOIN Regions r ON r.RegionID = er.RegionID
        LEFT JOIN Shippers sh ON sh.ShipperID = o.ShipVia
        LEFT JOIN "Order Details" od ON od.OrderID = o.OrderID
        GROUP BY o.OrderID
        """,
        columns=[
            Column("order_id", "integer", "Order identifier"),
            Column("order_date", "text", "Order date (ISO, YYYY-MM-DD)"),
            Column("order_month", "text", "Order month (YYYY-MM)"),
            Column("order_year", "text", "Order year (YYYY)"),
            Column("required_date", "text", "Date the customer required delivery by (ISO)"),
            Column("shipped_date", "text", "Date shipped (ISO); NULL when not yet shipped"),
            Column("is_shipped", "integer", "1 when the order has shipped, else 0"),
            Column("days_to_ship", "integer", "Days from order to shipment; NULL when not shipped"),
            Column("shipped_late", "integer", "1 when shipped after required_date, else 0"),
            *_CUSTOMER_COLS,
            *_EMPLOYEE_COLS,
            *_REGION_COLS,
            Column("shipper_name", "text", "Carrier: Speedy Express, United Package or Federal Shipping"),
            Column("ship_city", "text", "Delivery city"),
            Column("ship_country", "text", "Delivery country"),
            Column("freight", "real", "Freight charge for the order"),
            Column("line_count", "integer", "Number of order lines"),
            Column("total_units", "integer", "Units across all lines"),
            Column("order_total", "real", "Net order value after line discounts (revenue)"),
        ],
    ),
    ViewDef(
        name="v_order_lines",
        description="Order line items: each product sold on each order, with price, quantity, discount and net amount.",
        business_rules=(
            "line_total = unit_price * quantity * (1 - discount) is the net revenue of the line; gross_amount is before "
            "discount and discount_amount the difference. unit_price is the price at the time of the order (products "
            "may have a different list price now). Join to v_orders on order_id for order-level fields."
        ),
        sql=f"""
        SELECT o.OrderID AS order_id,
               DATE(o.OrderDate) AS order_date,
               strftime('%Y-%m', o.OrderDate) AS order_month,
               strftime('%Y', o.OrderDate) AS order_year,
               c.CustomerID AS customer_id, c.CompanyName AS customer_name, c.Country AS customer_country,
               e.EmployeeID AS employee_id, e.FirstName || ' ' || e.LastName AS employee_name,
               r.RegionID AS region_id, r.RegionDescription AS region_name,
               p.ProductID AS product_id, p.ProductName AS product_name, cat.CategoryName AS category_name,
               s.CompanyName AS supplier_name,
               od.UnitPrice AS unit_price, od.Quantity AS quantity, od.Discount AS discount,
               ROUND(od.UnitPrice * od.Quantity, 2) AS gross_amount,
               ROUND(od.UnitPrice * od.Quantity * od.Discount, 2) AS discount_amount,
               ROUND(od.UnitPrice * od.Quantity * (1 - od.Discount), 2) AS line_total
        FROM "Order Details" od
        CROSS JOIN Orders o ON o.OrderID = od.OrderID
        CROSS JOIN Customers c ON c.CustomerID = o.CustomerID
        CROSS JOIN Employees e ON e.EmployeeID = o.EmployeeID
        CROSS JOIN {_EMPLOYEE_REGION} er ON er.EmployeeID = e.EmployeeID
        CROSS JOIN Regions r ON r.RegionID = er.RegionID
        CROSS JOIN Products p ON p.ProductID = od.ProductID
        CROSS JOIN Categories cat ON cat.CategoryID = p.CategoryID
        CROSS JOIN Suppliers s ON s.SupplierID = p.SupplierID
        """,
        columns=[
            Column("order_id", "integer", "Order identifier"),
            Column("order_date", "text", "Order date (ISO, YYYY-MM-DD)"),
            Column("order_month", "text", "Order month (YYYY-MM)"),
            Column("order_year", "text", "Order year (YYYY)"),
            *_CUSTOMER_COLS,
            *_EMPLOYEE_COLS,
            *_REGION_COLS,
            *_PRODUCT_COLS,
            Column("supplier_name", "text", "Supplier of the product"),
            Column("unit_price", "real", "Unit price charged on this line"),
            Column("quantity", "integer", "Units sold"),
            Column("discount", "real", "Line discount as a fraction (0.15 = 15%)"),
            Column("gross_amount", "real", "unit_price * quantity"),
            Column("discount_amount", "real", "Discount given on the line"),
            Column("line_total", "real", "Net line revenue after discount"),
        ],
    ),
    ViewDef(
        name="v_monthly_sales",
        description="Sales aggregated per month, region and employee: orders, units, revenue and freight.",
        business_rules=(
            "One row per (order_month, employee). revenue = SUM of net line totals for orders placed that month. "
            "Use for time-series questions; SUM across employees for a region or company total. order_month sorts "
            "chronologically as text."
        ),
        sql=f"""
        SELECT strftime('%Y-%m', o.OrderDate) AS order_month,
               strftime('%Y', o.OrderDate) AS order_year,
               r.RegionID AS region_id, r.RegionDescription AS region_name,
               e.EmployeeID AS employee_id, e.FirstName || ' ' || e.LastName AS employee_name,
               COUNT(*) AS orders,
               COALESCE(SUM(t.units), 0) AS units,
               ROUND(COALESCE(SUM(t.revenue), 0), 2) AS revenue,
               ROUND(SUM(o.Freight), 2) AS freight
        FROM Orders o
        CROSS JOIN Employees e ON e.EmployeeID = o.EmployeeID
        CROSS JOIN {_EMPLOYEE_REGION} er ON er.EmployeeID = e.EmployeeID
        CROSS JOIN Regions r ON r.RegionID = er.RegionID
        LEFT JOIN (SELECT OrderID, SUM(Quantity) AS units, SUM(UnitPrice * Quantity * (1 - Discount)) AS revenue
                   FROM "Order Details" GROUP BY OrderID) t ON t.OrderID = o.OrderID
        GROUP BY strftime('%Y-%m', o.OrderDate), e.EmployeeID
        """,
        columns=[
            Column("order_month", "text", "Month (YYYY-MM)"),
            Column("order_year", "text", "Year (YYYY)"),
            *_REGION_COLS,
            *_EMPLOYEE_COLS,
            Column("orders", "integer", "Orders placed"),
            Column("units", "integer", "Units sold"),
            Column("revenue", "real", "Net revenue"),
            Column("freight", "real", "Freight charged on those orders"),
        ],
    ),
    ViewDef(
        name="v_category_sales",
        description="Sales per year, region and product category: orders, units and revenue.",
        business_rules=(
            "One row per (order_year, region, category). revenue is net of discounts. An order that spans several "
            "categories is counted once per category, so orders does not sum to the company order count."
        ),
        sql=f"""
        SELECT strftime('%Y', o.OrderDate) AS order_year,
               r.RegionID AS region_id, r.RegionDescription AS region_name,
               cat.CategoryID AS category_id, cat.CategoryName AS category_name,
               COUNT(DISTINCT o.OrderID) AS orders,
               SUM(od.Quantity) AS units,
               ROUND(SUM(od.UnitPrice * od.Quantity * (1 - od.Discount)), 2) AS revenue
        FROM "Order Details" od
        CROSS JOIN Orders o ON o.OrderID = od.OrderID
        CROSS JOIN Employees e ON e.EmployeeID = o.EmployeeID
        CROSS JOIN {_EMPLOYEE_REGION} er ON er.EmployeeID = e.EmployeeID
        CROSS JOIN Regions r ON r.RegionID = er.RegionID
        CROSS JOIN Products p ON p.ProductID = od.ProductID
        CROSS JOIN Categories cat ON cat.CategoryID = p.CategoryID
        GROUP BY strftime('%Y', o.OrderDate), r.RegionID, cat.CategoryID
        """,
        columns=[
            Column("order_year", "text", "Year (YYYY)"),
            *_REGION_COLS,
            Column("category_id", "integer", "Category identifier"),
            Column("category_name", "text", "Product category"),
            Column("orders", "integer", "Orders containing the category"),
            Column("units", "integer", "Units sold"),
            Column("revenue", "real", "Net revenue"),
        ],
    ),
    ViewDef(
        name="v_customer_sales",
        description="Lifetime sales per customer and region: orders, units, revenue, first and last order dates.",
        business_rules=(
            "One row per (customer, region): a customer served by employees from two regions appears twice, so SUM "
            "revenue GROUP BY customer for a customer total. revenue is net of discounts. Use v_customers for "
            "customer reference fields."
        ),
        sql=f"""
        SELECT c.CustomerID AS customer_id, c.CompanyName AS customer_name,
               c.Country AS customer_country, c.City AS customer_city,
               r.RegionID AS region_id, r.RegionDescription AS region_name,
               COUNT(DISTINCT o.OrderID) AS orders,
               COALESCE(SUM(od.Quantity), 0) AS units,
               ROUND(COALESCE(SUM(od.UnitPrice * od.Quantity * (1 - od.Discount)), 0), 2) AS revenue,
               MIN(DATE(o.OrderDate)) AS first_order_date,
               MAX(DATE(o.OrderDate)) AS last_order_date
        FROM Orders o
        CROSS JOIN Customers c ON c.CustomerID = o.CustomerID
        CROSS JOIN Employees e ON e.EmployeeID = o.EmployeeID
        CROSS JOIN {_EMPLOYEE_REGION} er ON er.EmployeeID = e.EmployeeID
        CROSS JOIN Regions r ON r.RegionID = er.RegionID
        LEFT JOIN "Order Details" od ON od.OrderID = o.OrderID
        GROUP BY c.CustomerID, r.RegionID
        """,
        columns=[
            *_CUSTOMER_COLS,
            Column("customer_city", "text", "Customer's city"),
            *_REGION_COLS,
            Column("orders", "integer", "Orders placed"),
            Column("units", "integer", "Units bought"),
            Column("revenue", "real", "Net revenue"),
            Column("first_order_date", "text", "First order date (ISO)"),
            Column("last_order_date", "text", "Most recent order date (ISO)"),
        ],
    ),
    ViewDef(
        name="v_employee_sales",
        description="One row per sales employee: title, hire date, manager, region, territories and lifetime orders/revenue.",
        business_rules=(
            "revenue is net of discounts over every order the employee took. manager_name is who they report to "
            "(NULL for the head of sales). territories counts the sales territories assigned to them."
        ),
        sql=f"""
        SELECT e.EmployeeID AS employee_id, e.FirstName || ' ' || e.LastName AS employee_name,
               e.Title AS title, DATE(e.HireDate) AS hire_date, e.City AS city, e.Country AS country,
               m.FirstName || ' ' || m.LastName AS manager_name,
               r.RegionID AS region_id, r.RegionDescription AS region_name,
               (SELECT COUNT(*) FROM EmployeeTerritories et WHERE et.EmployeeID = e.EmployeeID) AS territories,
               (SELECT COUNT(*) FROM Orders o WHERE o.EmployeeID = e.EmployeeID) AS orders,
               (SELECT ROUND(COALESCE(SUM(od.UnitPrice * od.Quantity * (1 - od.Discount)), 0), 2)
                FROM Orders o JOIN "Order Details" od ON od.OrderID = o.OrderID
                WHERE o.EmployeeID = e.EmployeeID) AS revenue
        FROM Employees e
        CROSS JOIN {_EMPLOYEE_REGION} er ON er.EmployeeID = e.EmployeeID
        CROSS JOIN Regions r ON r.RegionID = er.RegionID
        LEFT JOIN Employees m ON m.EmployeeID = e.ReportsTo
        """,
        columns=[
            *_EMPLOYEE_COLS,
            Column("title", "text", "Job title"),
            Column("hire_date", "text", "Hire date (ISO)"),
            Column("city", "text", "Office city"),
            Column("country", "text", "Office country"),
            Column("manager_name", "text", "Manager's name; NULL for the top of the org"),
            *_REGION_COLS,
            Column("territories", "integer", "Sales territories assigned"),
            Column("orders", "integer", "Lifetime orders taken"),
            Column("revenue", "real", "Lifetime net revenue"),
        ],
    ),
    ViewDef(
        name="v_shipping_performance",
        description="Shipping outcomes per year, region and shipper: orders, average freight, average days to ship, late orders.",
        business_rules=(
            "late_orders counts orders shipped after their required date; late_rate = late_orders / shipped_orders. "
            "avg_days_to_ship is over shipped orders only. Use for carrier comparisons and delivery-time trends."
        ),
        sql=f"""
        SELECT strftime('%Y', o.OrderDate) AS order_year,
               r.RegionID AS region_id, r.RegionDescription AS region_name,
               sh.ShipperID AS shipper_id, sh.CompanyName AS shipper_name,
               COUNT(*) AS orders,
               SUM(CASE WHEN o.ShippedDate IS NOT NULL THEN 1 ELSE 0 END) AS shipped_orders,
               ROUND(AVG(o.Freight), 2) AS avg_freight,
               ROUND(AVG(CASE WHEN o.ShippedDate IS NOT NULL
                              THEN julianday(DATE(o.ShippedDate)) - julianday(DATE(o.OrderDate)) END), 1)
                   AS avg_days_to_ship,
               SUM(CASE WHEN o.ShippedDate IS NOT NULL AND DATE(o.ShippedDate) > DATE(o.RequiredDate) THEN 1 ELSE 0 END)
                   AS late_orders,
               ROUND(CAST(SUM(CASE WHEN o.ShippedDate IS NOT NULL AND DATE(o.ShippedDate) > DATE(o.RequiredDate)
                                   THEN 1 ELSE 0 END) AS REAL)
                     / MAX(SUM(CASE WHEN o.ShippedDate IS NOT NULL THEN 1 ELSE 0 END), 1), 4) AS late_rate
        FROM Orders o
        CROSS JOIN Employees e ON e.EmployeeID = o.EmployeeID
        CROSS JOIN {_EMPLOYEE_REGION} er ON er.EmployeeID = e.EmployeeID
        CROSS JOIN Regions r ON r.RegionID = er.RegionID
        CROSS JOIN Shippers sh ON sh.ShipperID = o.ShipVia
        GROUP BY strftime('%Y', o.OrderDate), r.RegionID, sh.ShipperID
        """,
        columns=[
            Column("order_year", "text", "Year (YYYY)"),
            *_REGION_COLS,
            Column("shipper_id", "integer", "Shipper identifier"),
            Column("shipper_name", "text", "Carrier name"),
            Column("orders", "integer", "Orders assigned to the carrier"),
            Column("shipped_orders", "integer", "Orders actually shipped"),
            Column("avg_freight", "real", "Average freight charge"),
            Column("avg_days_to_ship", "real", "Average days from order to shipment"),
            Column("late_orders", "integer", "Orders shipped after the required date"),
            Column("late_rate", "real", "late_orders / shipped_orders (fraction)"),
        ],
    ),
    ViewDef(
        name="v_products",
        description="Product reference list: category, supplier, pack size, list price, stock, reorder level, discontinued flag.",
        business_rules=(
            "Region-agnostic reference data. is_discontinued = 1 means no longer sold. is_low_stock = 1 when "
            "units_in_stock <= reorder_level for an active product ('needs reordering'). unit_price here is the "
            "current list price; use v_order_lines.unit_price for what was actually charged."
        ),
        scope_column=None,
        sql="""
        SELECT p.ProductID AS product_id, p.ProductName AS product_name,
               cat.CategoryID AS category_id, cat.CategoryName AS category_name,
               s.SupplierID AS supplier_id, s.CompanyName AS supplier_name,
               p.QuantityPerUnit AS quantity_per_unit,
               p.UnitPrice AS unit_price, p.UnitsInStock AS units_in_stock, p.UnitsOnOrder AS units_on_order,
               p.ReorderLevel AS reorder_level,
               CASE WHEN p.Discontinued = '1' THEN 1 ELSE 0 END AS is_discontinued,
               CASE WHEN p.Discontinued = '0' AND p.UnitsInStock <= p.ReorderLevel THEN 1 ELSE 0 END AS is_low_stock
        FROM Products p
        CROSS JOIN Categories cat ON cat.CategoryID = p.CategoryID
        CROSS JOIN Suppliers s ON s.SupplierID = p.SupplierID
        """,
        columns=[
            *_PRODUCT_COLS,
            Column("category_id", "integer", "Category identifier"),
            Column("supplier_id", "integer", "Supplier identifier"),
            Column("supplier_name", "text", "Supplier company name"),
            Column("quantity_per_unit", "text", "Pack description, e.g. '10 boxes x 20 bags'"),
            Column("unit_price", "real", "Current list price"),
            Column("units_in_stock", "integer", "Units on hand"),
            Column("units_on_order", "integer", "Units on order from the supplier"),
            Column("reorder_level", "integer", "Reorder point"),
            Column("is_discontinued", "integer", "1 when discontinued, else 0"),
            Column("is_low_stock", "integer", "1 when an active product is at or below its reorder level"),
        ],
    ),
    ViewDef(
        name="v_customers",
        description="Customer reference list: company, contact, city, country and geographic region.",
        business_rules=(
            "Region-agnostic reference data (customer_region is the customer's geographic area, e.g. 'Western "
            "Europe', not a sales region). Contact details are personal data and are not exposed. For sales figures "
            "join v_customer_sales or v_orders on customer_id."
        ),
        scope_column=None,
        sql="""
        SELECT c.CustomerID AS customer_id, c.CompanyName AS customer_name,
               c.ContactName AS contact_name, c.ContactTitle AS contact_title,
               c.City AS city, c.Region AS customer_region, c.Country AS country, c.Phone AS phone
        FROM Customers c
        """,
        columns=[
            Column("customer_id", "text", "Customer identifier (5-letter code)"),
            Column("customer_name", "text", "Customer company name"),
            Column("contact_name", "text", "Contact person (personal data)", sensitive=True),
            Column("contact_title", "text", "Contact's job title"),
            Column("city", "text", "City"),
            Column("customer_region", "text", "Geographic area (e.g. Western Europe, North America)"),
            Column("country", "text", "Country"),
            Column("phone", "text", "Contact phone (personal data)", sensitive=True),
        ],
    ),
    ViewDef(
        name="v_suppliers",
        description="Supplier reference list with city, country and how many products each one supplies.",
        business_rules="Region-agnostic reference data. Contact details are personal data and are not exposed.",
        scope_column=None,
        sql="""
        SELECT s.SupplierID AS supplier_id, s.CompanyName AS supplier_name,
               s.ContactName AS contact_name, s.City AS city, s.Country AS country, s.Phone AS phone,
               COUNT(p.ProductID) AS products_supplied
        FROM Suppliers s
        LEFT JOIN Products p ON p.SupplierID = s.SupplierID
        GROUP BY s.SupplierID
        """,
        columns=[
            Column("supplier_id", "integer", "Supplier identifier"),
            Column("supplier_name", "text", "Supplier company name"),
            Column("contact_name", "text", "Contact person (personal data)", sensitive=True),
            Column("city", "text", "City"),
            Column("country", "text", "Country"),
            Column("phone", "text", "Contact phone (personal data)", sensitive=True),
            Column("products_supplied", "integer", "Number of products sourced from this supplier"),
        ],
    ),
]

VIEW_NAMES = [v.name for v in VIEWS]
