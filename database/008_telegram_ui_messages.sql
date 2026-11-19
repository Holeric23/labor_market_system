-- Only the current temporary interface message is tracked; vacancy messages are never stored here.
CREATE TABLE telegram_ui_messages (
    chat_id bigint PRIMARY KEY CHECK (chat_id > 0),
    message_id bigint CHECK (message_id > 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);
