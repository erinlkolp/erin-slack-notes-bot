CREATE DATABASE IF NOT EXISTS slack_notes;

USE slack_notes;

CREATE TABLE IF NOT EXISTS notes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    username VARCHAR(255),
    note_text TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    channel_id VARCHAR(255),
    channel_name VARCHAR(255)
);
