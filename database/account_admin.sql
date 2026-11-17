-- Soft-deleted accounts: views and procedures for MySQL Workbench. The app installs this file
-- automatically on start-up. Soft-deleted ids are in the deleted_accounts table.
--   CALL restore_account('u_...');     undo a soft delete
--   CALL purge_account('u_...');       permanently delete one soft-deleted account and its data
--   CALL purge_deleted_accounts(30);   permanently delete all soft-deleted 30+ days ago (0 = all)

CREATE OR REPLACE VIEW accounts_overview AS
SELECT p.id AS profile_id, p.display_name AS name, COALESCE(p.email, p.deleted_email) AS email, p.is_deleted,
       IF(p.is_deleted = 1, 'deleted', 'active') AS status, p.created_at, p.deleted_at,
       w.id AS workspace_id, w.name AS budget
FROM profiles p
LEFT JOIN memberships m ON m.profile_id = p.id
LEFT JOIN workspaces w ON w.id = m.workspace_id
WHERE p.is_demo = 0;

CREATE OR REPLACE VIEW deleted_account_transactions AS
SELECT p.id AS profile_id, p.deleted_email AS email, t.id AS transaction_id, t.posted_on, t.payee,
       t.description, t.category_id, t.kind, t.amount / 100 AS amount_rupees
FROM profiles p
JOIN memberships m ON m.profile_id = p.id
JOIN transactions t ON t.workspace_id = m.workspace_id
WHERE p.is_deleted = 1 AND p.is_demo = 0;

DELIMITER $$

-- Older versions had a helper procedure; purge_account does its job now.
DROP PROCEDURE IF EXISTS purge_account_rows $$

DROP PROCEDURE IF EXISTS restore_account $$
CREATE PROCEDURE restore_account(IN p_profile_id VARCHAR(32))
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
  UPDATE profiles SET email = v_email, deleted_email = NULL, is_deleted = 0, deleted_at = NULL
   WHERE id = p_profile_id;
  UPDATE workspaces SET is_deleted = 0, deleted_at = NULL
   WHERE is_demo = 0 AND id IN (SELECT workspace_id FROM memberships WHERE profile_id = p_profile_id);
  DELETE FROM deleted_accounts WHERE profile_id = p_profile_id;
  COMMIT;
  SELECT CONCAT('Restored account ', p_profile_id) AS result;
END $$

DROP PROCEDURE IF EXISTS purge_account $$
CREATE PROCEDURE purge_account(IN p_profile_id VARCHAR(32))
BEGIN
  DECLARE v_found INT DEFAULT 0;
  DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN ROLLBACK; DROP TEMPORARY TABLE IF EXISTS purge_ws; RESIGNAL; END;

  START TRANSACTION;
  SELECT COUNT(*) INTO v_found
    FROM profiles WHERE id = p_profile_id AND is_deleted = 1 AND is_demo = 0 FOR UPDATE;
  IF v_found = 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Only soft-deleted accounts can be purged (see deleted_accounts).';
  END IF;
  -- Budgets owned by this account alone. Deleting them removes their transactions, bills, budgets,
  -- goals and imports (ON DELETE CASCADE); the audit log has no foreign keys, so it is cleared here.
  DROP TEMPORARY TABLE IF EXISTS purge_ws;
  CREATE TEMPORARY TABLE purge_ws (id INT PRIMARY KEY);
  INSERT INTO purge_ws
  SELECT m.workspace_id FROM memberships m JOIN workspaces w ON w.id = m.workspace_id
   WHERE m.profile_id = p_profile_id AND w.is_demo = 0
     AND NOT EXISTS (SELECT 1 FROM memberships o WHERE o.workspace_id = m.workspace_id AND o.profile_id <> p_profile_id);
  DELETE FROM deleted_accounts WHERE profile_id = p_profile_id;
  DELETE FROM audit_log WHERE profile_id = p_profile_id OR workspace_id IN (SELECT id FROM purge_ws);
  DELETE FROM workspaces WHERE id IN (SELECT id FROM purge_ws);
  DELETE FROM profiles WHERE id = p_profile_id;
  DROP TEMPORARY TABLE purge_ws;
  COMMIT;
END $$

DROP PROCEDURE IF EXISTS purge_deleted_accounts $$
CREATE PROCEDURE purge_deleted_accounts(IN p_older_than_days INT)
BEGIN
  DECLARE v_done INT DEFAULT 0;
  DECLARE v_id VARCHAR(32);
  DECLARE v_count INT DEFAULT 0;
  DECLARE cur CURSOR FOR SELECT id FROM purge_ids;
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_done = 1;

  IF p_older_than_days IS NULL OR p_older_than_days < 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Give a number of days, 0 or more (0 = every soft-deleted account).';
  END IF;
  DROP TEMPORARY TABLE IF EXISTS purge_ids;   -- list first, so the loop never reads what it deletes
  CREATE TEMPORARY TABLE purge_ids (id VARCHAR(32) PRIMARY KEY);
  INSERT INTO purge_ids
  SELECT d.profile_id FROM deleted_accounts d
    JOIN profiles p ON p.id = d.profile_id AND p.is_deleted = 1 AND p.is_demo = 0
   WHERE d.deleted_at <= NOW() - INTERVAL p_older_than_days DAY;
  OPEN cur;
  purge_loop: LOOP
    FETCH cur INTO v_id;
    IF v_done = 1 THEN LEAVE purge_loop; END IF;
    CALL purge_account(v_id);
    SET v_count = v_count + 1;
  END LOOP;
  CLOSE cur;
  DROP TEMPORARY TABLE purge_ids;
  SELECT CONCAT('Permanently deleted ', v_count, ' soft-deleted account(s).') AS result;
END $$

DELIMITER ;
