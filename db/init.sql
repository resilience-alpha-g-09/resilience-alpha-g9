-- RESILIENCE-ALPHA — schéma du SI de paiement fictif. Données 100 % fictives.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
    id            SERIAL PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'customer' CHECK (role IN ('customer','merchant','service')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE accounts (
    id         SERIAL PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES users(id),
    label      TEXT NOT NULL,
    balance    NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (balance >= 0),
    currency   CHAR(3) NOT NULL DEFAULT 'EUR'
);

CREATE TABLE transactions (
    id             BIGSERIAL PRIMARY KEY,
    from_account   INT NOT NULL REFERENCES accounts(id),
    to_account     INT NOT NULL REFERENCES accounts(id),
    amount         NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    status         TEXT NOT NULL CHECK (status IN ('completed','rejected')),
    reason         TEXT,
    origin         TEXT NOT NULL DEFAULT 'web',   -- web | simulator | verifier
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX transactions_from_idx ON transactions (from_account, created_at DESC);
CREATE INDEX transactions_created_idx ON transactions (created_at DESC);

-- Comptes de démo. Mots de passe : DEMO_PASSWORD (.env) pour les humains et simulateurs,
-- VERIFIER_PASSWORD pour le compte de service du remédiateur.
\set demo_pw `echo "$DEMO_PASSWORD"`
\set verifier_pw `echo "$VERIFIER_PASSWORD"`

INSERT INTO users (email, password_hash, role) VALUES
  ('alice@demo.local',      crypt(:'demo_pw', gen_salt('bf', 10)), 'customer'),   -- id 1
  ('bob@demo.local',        crypt(:'demo_pw', gen_salt('bf', 10)), 'customer'),   -- id 2
  ('carol@demo.local',      crypt(:'demo_pw', gen_salt('bf', 10)), 'customer'),   -- id 3
  ('sim1@demo.local',       crypt(:'demo_pw', gen_salt('bf', 10)), 'customer'),   -- id 4
  ('sim2@demo.local',       crypt(:'demo_pw', gen_salt('bf', 10)), 'customer'),   -- id 5
  ('sim3@demo.local',       crypt(:'demo_pw', gen_salt('bf', 10)), 'customer'),   -- id 6
  ('sim4@demo.local',       crypt(:'demo_pw', gen_salt('bf', 10)), 'customer'),   -- id 7
  ('sim5@demo.local',       crypt(:'demo_pw', gen_salt('bf', 10)), 'customer'),   -- id 8
  ('verifier@internal.local', crypt(:'verifier_pw', gen_salt('bf', 10)), 'service'), -- id 9
  ('shop@demo.local',       crypt(:'demo_pw', gen_salt('bf', 10)), 'merchant');   -- id 10

INSERT INTO accounts (user_id, label, balance) VALUES
  (1, 'Alice — courant',      5000.00),     -- account 1
  (2, 'Bob — courant',        5000.00),     -- account 2
  (3, 'Carol — courant',      5000.00),     -- account 3
  (4, 'sim1',             10000000.00),     -- account 4
  (5, 'sim2',             10000000.00),     -- account 5
  (6, 'sim3',             10000000.00),     -- account 6
  (7, 'sim4',             10000000.00),     -- account 7
  (8, 'sim5',             10000000.00),     -- account 8
  (9, 'verifier',          1000000.00),     -- account 9
  (10,'Boutique fictive',         0.00);    -- account 10 (destinataire marchand)
