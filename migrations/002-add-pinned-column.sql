--liquibase formatted sql

--changeset erin:3
--comment: Add pinned column to notes table
ALTER TABLE notes ADD COLUMN pinned TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE notes ADD INDEX idx_pinned (pinned);
