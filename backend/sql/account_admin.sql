-- =============================================================================================
--  Account administration: soft-deleted accounts, restore and permanent (hard) delete
-- =============================================================================================
--
--  How soft delete works
--    When a person deletes their account in the app, nothing is erased:
--      profiles.is_deleted   0 = active, 1 = deleted        (workspaces.is_deleted likewise for the budget)
--      profiles.deleted_at   when it happened
--      profiles.deleted_email  their email, moved aside so the address can sign up again
--    Every transaction, bill, budget and goal stays in the database.
--
--  The app installs everything below automatically when it starts on MySQL, so you can use it
--  straight away in MySQL Workbench (schema hci_finance -> Views / Stored Procedures).
--
--  Everyday use (run in Workbench with hci_finance as the default schema)
--
--    SELECT * FROM accounts_overview;                     -- every real account, active or deleted
--    SELECT * FROM deleted_accounts;                      -- only soft-deleted ones, with how much data each has
--    SELECT * FROM deleted_account_transactions
--      WHERE profile_id = 'u_...';                        -- the kept records of one deleted account
--
--    CALL restore_account('u_...');                       -- undo a soft delete (they can log in again)
--    CALL purge_account('u_...');                         -- PERMANENT: delete one soft-deleted account and its data
--    CALL purge_deleted_accounts(30);                     -- PERMANENT: every account soft-deleted 30+ days ago
--    CALL purge_deleted_accounts(0);                      -- PERMANENT: every soft-deleted account
--
--  Safety
--    * Only accounts with is_deleted = 1 can be purged; active and demo accounts are refused.
--    * Each call is one transaction: if anything fails, nothing is changed.
--    * Restore is refused if the email has since been used for a new account.
-- =============================================================================================


-- ---------------------------------------------------------------------------------- views --

CREATE OR REPLACE VIEW accounts_overview AS
SELECT p.id                                   AS profile_id,
       p.display_name                         AS name,
       COALESCE(p.email, p.deleted_email)     AS email,
       p.is_deleted,
       CASE WHEN p.is_deleted = 1 THEN 'deleted' ELSE 'active' END AS status,
       p.created_at,
       p.deleted_at,
       w.id                                   AS workspace_id,
       w.name                                 AS budget
FROM profiles p
LEFT JOIN memberships m ON m.profile_id = p.id
LEFT JOIN workspaces  w ON w.id = m.workspace_id
WHERE p.is_demo = 0;

CREATE OR REPLACE VIEW deleted_accounts AS
SELECT p.id                                   AS profile_id,
       p.display_name                         AS name,
       p.deleted_email                        AS email,
       p.deleted_at,
       DATEDIFF(CURRENT_DATE, p.deleted_at)   AS days_since_deleted,
       w.id                                   AS workspace_id,
       w.name                                 AS budget,
       (SELECT COUNT(*) FROM transactions    x WHERE x.workspace_id = w.id) AS transactions,
       (SELECT COUNT(*) FROM recurring_bills x WHERE x.workspace_id = w.id) AS recurring_items,
       (SELECT COUNT(*) FROM budgets         x WHERE x.workspace_id = w.id) AS budget_limits,
       (SELECT COUNT(*) FROM goals           x WHERE x.workspace_id = w.id) AS savings_goals
FROM profiles p
LEFT JOIN memberships m ON m.profile_id = p.id
LEFT JOIN workspaces  w ON w.id = m.workspace_id
WHERE p.is_deleted = 1 AND p.is_demo = 0;

CREATE OR REPLACE VIEW deleted_account_transactions AS
SELECT p.id              AS profile_id,
       p.deleted_email   AS email,
       t.id              AS transaction_id,
       t.posted_on,
       t.payee,
       t.description,
       t.category_id,
       t.kind,
       t.amount / 100    AS amount_rupees
FROM profiles p
JOIN memberships  m ON m.profile_id = p.id
JOIN transactions t ON t.workspace_id = m.workspace_id
WHERE p.is_deleted = 1 AND p.is_demo = 0;


-- ----------------------------------------------------------------------------- procedures --

DELIMITER $$

DROP PROCEDURE IF EXISTS restore_account $$
CREATE PROCEDURE restore_account(IN p_profile_id VARCHAR(32))
COMMENT 'Undo a soft delete: the account can log in again and its budget reopens.'
BEGIN
  DECLARE v_found INT DEFAULT 0;
  DECLARE v_email VARCHAR(254);
  DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN ROLLBACK; RESIGNAL; END;

  START TRANSACTION;
  SELECT COUNT(*), MAX(deleted_email) INTO v_found, v_email
    FROM profiles WHERE id = p_profile_id AND is_deleted = 1 AND is_demo = 0 FOR UPDATE;
  IF v_found = 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'No soft-deleted account has that profile_id.';
  END IF;
  IF v_email IS NOT NULL AND EXISTS (SELECT 1 FROM profiles WHERE email = v_email) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Restore refused: that email now belongs to another account.';
  END IF;

  UPDATE profiles
     SET email = v_email, deleted_email = NULL, is_deleted = 0, deleted_at = NULL
   WHERE id = p_profile_id;
  UPDATE workspaces
     SET is_deleted = 0, deleted_at = NULL
   WHERE is_demo = 0 AND id IN (SELECT workspace_id FROM memberships WHERE profile_id = p_profile_id);
  COMMIT;
  SELECT CONCAT('Restored account ', p_profile_id, ' (', COALESCE(v_email, 'no email'), ').') AS result;
END $$

DROP PROCEDURE IF EXISTS purge_account_rows $$
CREATE PROCEDURE purge_account_rows(IN p_profile_id VARCHAR(32))
COMMENT 'Internal: permanently delete one soft-deleted account. Use purge_account().'
BEGIN
  DECLARE v_found INT DEFAULT 0;
  DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN ROLLBACK; DROP TEMPORARY TABLE IF EXISTS purge_ws; RESIGNAL; END;

  START TRANSACTION;
  SELECT COUNT(*) INTO v_found
    FROM profiles WHERE id = p_profile_id AND is_deleted = 1 AND is_demo = 0 FOR UPDATE;
  IF v_found = 0 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'Only soft-deleted accounts can be purged. Check the profile_id in deleted_accounts.';
  END IF;

  -- Budgets that belong to this account alone (never a demo budget or one shared with someone else).
  DROP TEMPORARY TABLE IF EXISTS purge_ws;
  CREATE TEMPORARY TABLE purge_ws (id INT PRIMARY KEY);
  INSERT INTO purge_ws
  SELECT m.workspace_id FROM memberships m JOIN workspaces w ON w.id = m.workspace_id
   WHERE m.profile_id = p_profile_id AND w.is_demo = 0
     AND NOT EXISTS (SELECT 1 FROM memberships o
                      WHERE o.workspace_id = m.workspace_id AND o.profile_id <> p_profile_id);

  -- The audit log has no foreign keys, so it is cleared explicitly.  Deleting a workspace removes its
  -- transactions, bills, budgets, goals, imports, scenarios and memberships (ON DELETE CASCADE);
  -- deleting the profile removes its preferences.
  DELETE FROM audit_log  WHERE profile_id = p_profile_id OR workspace_id IN (SELECT id FROM purge_ws);
  DELETE FROM workspaces WHERE id IN (SELECT id FROM purge_ws);
  DELETE FROM profiles   WHERE id = p_profile_id;
  DROP TEMPORARY TABLE purge_ws;
  COMMIT;
END $$

DROP PROCEDURE IF EXISTS purge_account $$
CREATE PROCEDURE purge_account(IN p_profile_id VARCHAR(32))
COMMENT 'PERMANENT: delete one soft-deleted account and all of its data.'
BEGIN
  CALL purge_account_rows(p_profile_id);
  SELECT CONCAT('Permanently deleted account ', p_profile_id, ' and its data.') AS result;
END $$

DROP PROCEDURE IF EXISTS purge_deleted_accounts $$
CREATE PROCEDURE purge_deleted_accounts(IN p_older_than_days INT)
COMMENT 'PERMANENT: delete every account soft-deleted at least this many days ago (0 = all).'
BEGIN
  DECLARE v_done INT DEFAULT 0;
  DECLARE v_id VARCHAR(32);
  DECLARE v_count INT DEFAULT 0;
  DECLARE cur CURSOR FOR SELECT id FROM purge_ids;
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_done = 1;

  IF p_older_than_days IS NULL OR p_older_than_days < 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Give a number of days, 0 or more (0 = every soft-deleted account).';
  END IF;

  -- Take the list first, so the loop never reads a table it is deleting from.
  DROP TEMPORARY TABLE IF EXISTS purge_ids;
  CREATE TEMPORARY TABLE purge_ids (id VARCHAR(32) PRIMARY KEY);
  INSERT INTO purge_ids
  SELECT id FROM profiles
   WHERE is_deleted = 1 AND is_demo = 0 AND deleted_at <= NOW() - INTERVAL p_older_than_days DAY;

  OPEN cur;
  purge_loop: LOOP
    FETCH cur INTO v_id;
    IF v_done = 1 THEN LEAVE purge_loop; END IF;
    CALL purge_account_rows(v_id);   -- each account in its own transaction
    SET v_count = v_count + 1;
  END LOOP;
  CLOSE cur;
  DROP TEMPORARY TABLE purge_ids;
  SELECT CONCAT('Permanently deleted ', v_count, ' soft-deleted account(s).') AS result;
END $$

DELIMITER ;
