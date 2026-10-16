# Hostel Management System

A college DBMS learning project: design and build a hostel management system one feature at a time. The goal is to understand relational modeling, SQL, constraints, joins, transactions, and database-backed application code by using them in a practical domain.

This repository currently contains the project guide only. Build the application incrementally by following the milestones below.

## Project Goal

Manage the core records and workflows for a hostel:

- Students and their contact details
- Hostels, rooms, and room capacity
- Student room allocations and check-in/check-out dates
- Monthly charges and payment records
- Reports for occupancy, outstanding balances, and revenue

## Recommended Stack

- MySQL 8.0 or newer for the relational database
- Python 3.10 or newer with Flask for the web application
- HTML templates for a simple interface
- Git and GitHub for version control

Start with SQL and the schema before building the web interface. You can complete the database portion independently as a DBMS assignment.

## macOS Setup

Install MySQL with Homebrew:

```sh
brew install mysql
brew services start mysql
```

Check that the server is available:

```sh
mysql --version
mysql -u root
```

If the MySQL command-line client asks for a password, use `mysql -u root -p` instead. Create a database for the project:

```sql
CREATE DATABASE hostel_management;
USE hostel_management;
```

For the later Flask stage, create a virtual environment and install the starter dependencies:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install Flask mysql-connector-python python-dotenv
```

Keep database credentials in a local `.env` file once you add application code. Do not commit credentials or real student information to GitHub.

## Build Milestones

1. **Requirements:** List the information the hostel must store and the reports staff need. Decide which rules the database must enforce.
2. **ER model:** Identify entities such as `Student`, `Room`, `Allocation`, and `Payment`. Draw their relationships and cardinalities before creating tables.
3. **Relational schema:** Define tables, primary keys, foreign keys, data types, `NOT NULL`, `UNIQUE`, and `CHECK` constraints. Store an allocation separately instead of repeating room details on every student row.
4. **Normalization:** Check the design against 1NF, 2NF, and 3NF. Explain how separate tables avoid update, insertion, and deletion anomalies.
5. **Sample data:** Add a few rooms and students, then create allocations and payments. Use realistic but fictional records only.
6. **SQL reports:** Write and test the queries below directly in MySQL.
7. **Application:** Add a Flask page to list records, then implement create/update workflows with parameterized SQL. Validate inputs in both the application and database where appropriate.
8. **Transactions and testing:** Make room allocation atomic. Test capacity limits, duplicate active allocations, invalid references, and payment amounts.
9. **Documentation:** Include your ER diagram, assumptions, normalization explanation, schema, sample queries, and screenshots or query results in your college submission.

## SQL Practice

- List each student with their active room allocation.
- Show every room and its current occupancy, including empty rooms.
- Find rooms with at least one available bed.
- Calculate monthly revenue using `SUM` and `GROUP BY`.
- Find active allocations with no payment in a selected month.
- Find students whose total payments are below the amount due.
- Show the top three rooms by occupancy.
- Use a transaction to allocate a student, and roll it back if the room is full.

Use these exercises to practice `INNER JOIN`, `LEFT JOIN`, aggregate functions, `GROUP BY`, `HAVING`, subqueries, indexes, and transactions. Keep the SQL scripts you write in a `database/` folder as the project grows.

## Suggested Repository Structure

```text
hostel-management-system/
├── README.md
├── database/
│   ├── schema.sql
│   ├── seed.sql
│   └── queries.sql
├── app/
│   ├── app.py
│   └── templates/
└── tests/
```

Create folders only as you reach those milestones; begin by completing the schema and SQL exercises.

## Create the GitHub Repository

From this project directory, initialize Git and make the first commit:

```sh
git init
git add README.md
git commit -m "Add hostel management DBMS project guide"
```

With GitHub CLI installed and authenticated (`gh auth login`), create and publish a public repository:

```sh
gh repo create hostel-management-system --public --source=. --remote=origin --push
```

Use `--private` instead of `--public` for a private repository. You can also create an empty repository on GitHub first, then follow GitHub's instructions to add it as the `origin` remote and push the `main` branch.

## Working Safely

Use fictional student data while learning. Keep passwords and `.env` files out of commits, and do not put personal information in a public repository.
