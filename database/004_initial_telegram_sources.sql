-- These channels were identified from the URLs of publications already stored
-- in the previous local project. No live availability check is implied.
CREATE TEMP TABLE initial_telegram_sources(name text, username text, publications integer) ON COMMIT DROP;
INSERT INTO initial_telegram_sources VALUES
    ('digital_jobster','digital_jobster',5471),
    ('vdhl','vdhl_good',22336),
    ('designhunters','designhunters',3811),
    ('uiux_job','uiux_jobs',43689),
    ('cg_freelance','cg_freelance',931),
    ('marketing_jobs','marketing_jobs',9547),
    ('rabota_go','rabota_go',4289),
    ('workasap','workasap',2960),
    ('dddwork','dddwork',4818),
    ('mediajobs_ru','mediajobs_ru',5816),
    ('norm_rabota','norm_rabota',94798),
    ('prwork','prwork',2351),
    ('gamedev_jobs','gamedev_jobs',51),
    ('talentedpeoples','talentedpeoples',27977),
    ('freelancetaverna','freelancetaverna',3634),
    ('smm_jobs','marketing_rabota_jobs',681),
    ('a-teams','huggabletalents',2520);

INSERT INTO sources(kind,name,address,enabled,schedule_enabled)
SELECT 'telegram',i.name,'https://t.me/'||i.username,true,false
FROM initial_telegram_sources i
WHERE NOT EXISTS (SELECT 1 FROM telegram_sources t WHERE t.username=i.username)
ON CONFLICT (kind,address) DO NOTHING;

UPDATE telegram_sources t SET provenance=jsonb_build_object(
    'origin','previous_project_publications',
    'previous_name',i.name,
    'historical_publications',i.publications
)
FROM initial_telegram_sources i
WHERE t.username=i.username AND t.provenance='{}'::jsonb;
