-- Run once in MySQL Workbench as root. Replace CHANGE_ME with a password (twice) and put the
-- same password in backend/.env. The app creates its tables itself on first run.

CREATE DATABASE IF NOT EXISTS hci_finance      CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS hci_finance_test CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

CREATE USER IF NOT EXISTS 'hci_app'@'localhost' IDENTIFIED BY 'CHANGE_ME';
ALTER USER 'hci_app'@'localhost' IDENTIFIED BY 'CHANGE_ME';
GRANT ALL PRIVILEGES ON hci_finance.*      TO 'hci_app'@'localhost';
GRANT ALL PRIVILEGES ON hci_finance_test.* TO 'hci_app'@'localhost';
FLUSH PRIVILEGES;
