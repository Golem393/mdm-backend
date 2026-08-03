-- Tracks customers who paid through anonymous checkout and still owe a
-- "create your password" email.
--
-- Stripe retries a webhook after any 5xx. The create-password email used to be gated on
-- "did this webhook attempt create the user", which is only ever true on the FIRST attempt
-- — the very one that failed. On the retry the user already existed, the gate went false,
-- and the email was never sent again: a paying customer with an account they cannot log
-- into, and nothing in the system saying so.
--
-- The flag is set when the user is created and cleared only after the email is actually
-- sent, so a retry always resumes exactly where it left off.
--
-- Operationally this doubles as an alert:
--   select email, created_at from profiles where password_email_pending;
-- is the list of customers who paid but are locked out.

alter table profiles
  add column if not exists password_email_pending boolean not null default false;

comment on column profiles.password_email_pending is
  'True between creating a checkout user and successfully sending their create-password email. Any row left true is a paying customer who cannot log in.';

-- Partial index: this is queried as "who is stuck", never as "who is fine".
create index if not exists profiles_password_email_pending
  on profiles (password_email_pending)
  where password_email_pending;
