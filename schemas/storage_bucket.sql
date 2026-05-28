-- Storage bucket dla pobranych PDFow ze skladami portfeli (dokumenty.analizy.pl).
--
-- Bucket private - dostepny tylko przez service_role key (z GH Actions / lokalnego
-- skryptu). Nie publikujemy PDFow zewnetrznie - analizy.pl ma swoje TOS, ponadto
-- to nasze "raw data backup".
--
-- Struktura sciezek w buckecie:
--   raw-pdfs/{parasol_code}/{YYYY-MM-DD}.pdf
--
-- Idempotent: INSERT ... ON CONFLICT DO NOTHING.

INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
    'raw-pdfs',
    'raw-pdfs',
    FALSE,                              -- private
    52428800,                           -- 50 MB limit per file (probka PKO Parasolowy ma ~400KB)
    ARRAY['application/pdf']
)
ON CONFLICT (id) DO UPDATE
SET file_size_limit = EXCLUDED.file_size_limit,
    allowed_mime_types = EXCLUDED.allowed_mime_types;

-- RLS policies dla bucketu raw-pdfs.
-- Service-role key omija RLS, wiec polityki są tutaj tylko jako safety net na
-- wypadek przypadkowego uzycia anon key. Zezwalamy tylko service_role + authenticated
-- (ale nie anon) na read; write tylko service_role.
--
-- Jezeli polityki juz istnieja, DROP+CREATE (Postgres nie ma CREATE POLICY IF NOT EXISTS).

DO $$
BEGIN
    -- READ policy
    IF EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'storage' AND policyname = 'raw_pdfs_read_authenticated') THEN
        DROP POLICY raw_pdfs_read_authenticated ON storage.objects;
    END IF;
    EXECUTE $POL$
        CREATE POLICY raw_pdfs_read_authenticated
        ON storage.objects FOR SELECT
        TO authenticated, service_role
        USING (bucket_id = 'raw-pdfs')
    $POL$;

    -- WRITE policy
    IF EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'storage' AND policyname = 'raw_pdfs_write_service_role') THEN
        DROP POLICY raw_pdfs_write_service_role ON storage.objects;
    END IF;
    EXECUTE $POL$
        CREATE POLICY raw_pdfs_write_service_role
        ON storage.objects FOR INSERT
        TO service_role
        WITH CHECK (bucket_id = 'raw-pdfs')
    $POL$;
END $$;
