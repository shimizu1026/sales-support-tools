-- デモ専用 Supabase プロジェクト用（SALES_DEMO_MODE=0 のとき）

-- SQL Editor に貼り付けて実行



CREATE TABLE IF NOT EXISTS users (

  username TEXT PRIMARY KEY

);



CREATE TABLE IF NOT EXISTS saved_companies (

  id BIGSERIAL PRIMARY KEY,

  username TEXT,

  saved_at TIMESTAMPTZ DEFAULT NOW(),

  company_name TEXT NOT NULL,

  address TEXT,

  tel TEXT,

  website_url TEXT,

  representative TEXT,

  capital TEXT,

  employees TEXT,

  established TEXT,

  business TEXT,

  industry TEXT,

  corporate_number TEXT,

  sales_history TEXT,

  csv_summary TEXT,

  csv_banks TEXT,

  csv_customers TEXT,

  csv_suppliers TEXT,

  csv_officers TEXT,

  csv_listing TEXT,

  csv_id TEXT,

  thumbnail_url TEXT,

  screenshot_path TEXT,

  philosophy TEXT,

  csv_philosophy TEXT,

  ai_point TEXT,

  weak_points TEXT,

  tech_stack TEXT,

  server_info TEXT,

  renewal_score TEXT,

  latitude TEXT,

  longitude TEXT,

  crm_status TEXT DEFAULT '未着手',

  crm_assignee TEXT,

  crm_memo TEXT,

  crm_updated_at TIMESTAMPTZ

);



CREATE INDEX IF NOT EXISTS idx_saved_company_name ON saved_companies (company_name);

CREATE INDEX IF NOT EXISTS idx_saved_status ON saved_companies (crm_status);

CREATE INDEX IF NOT EXISTS idx_saved_assignee ON saved_companies (crm_assignee);



ALTER TABLE saved_companies ENABLE ROW LEVEL SECURITY;

ALTER TABLE users ENABLE ROW LEVEL SECURITY;



-- デモ用: anon key から読み書き可（本番では厳格化すること）

CREATE POLICY "demo_anon_all_saved" ON saved_companies FOR ALL USING (true) WITH CHECK (true);

CREATE POLICY "demo_anon_all_users" ON users FOR ALL USING (true) WITH CHECK (true);

