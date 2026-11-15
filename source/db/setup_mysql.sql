-- Run ONCE in MySQL Workbench as root (File > Open SQL Script, then the lightning bolt).
-- 1. Replace CHANGE_ME below with a password you choose (twice).
-- 2. Put the same password in source/.env (copy .env.example).
-- This only creates the two project schemas and one user; nothing else is touched.

CREATE DATABASE IF NOT EXISTS hci_finance      CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS hci_finance_test CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

CREATE USER IF NOT EXISTS 'hci_app'@'localhost' IDENTIFIED BY 'CHANGE_ME';
ALTER USER 'hci_app'@'localhost' IDENTIFIED BY 'CHANGE_ME';

GRANT ALL PRIVILEGES ON hci_finance.*      TO 'hci_app'@'localhost';
GRANT ALL PRIVILEGES ON hci_finance_test.* TO 'hci_app'@'localhost';
FLUSH PRIVILEGES;

-- The tables are created by the app on first run (python app.py).
-- To look at the data afterwards:  USE hci_finance;  SELECT * FROM transactions LIMIT 50;
