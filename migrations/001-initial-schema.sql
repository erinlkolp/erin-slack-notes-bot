--liquibase formatted sql

--changeset erin:1
--comment: Create notes table
CREATE TABLE notes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    username VARCHAR(255),
    note_text TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    channel_id VARCHAR(255),
    channel_name VARCHAR(255),
    INDEX idx_user_created (user_id, created_at)
);

--changeset erin:2
--comment: Create note_tags table
CREATE TABLE note_tags (
    id INT AUTO_INCREMENT PRIMARY KEY,
    note_id INT NOT NULL,
    tag VARCHAR(255) NOT NULL,
    INDEX idx_tag (tag),
    INDEX idx_note_id (note_id),
    FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
);
