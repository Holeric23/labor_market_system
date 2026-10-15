"""Agent-authored provisional labels, written before running extractor predictions.

This is a small diagnostic silver reference, NOT human gold. Index refers to
pilot_sample.json frozen on 2026-09-30. No synthetic posts are included.
Skills: explicitly stated actionable competencies within the existing taxonomy;
vacancy duties alone are excluded, resume services count as possessed skills.
Occupations: target/self-described roles, specific role supersedes generic parent.
Salary: explicitly stated remuneration amounts, a deliberately limited subtask.
"""

# skills, occupations, salary amounts, unconditional work formats, taxonomy gaps
ANNOTATIONS = [
    ('smm target_ads content_strategy tilda figma', 'smm_manager designer targetologist', [], [], []),
    ('copywriting photoshop editing literacy', 'copywriter account_manager', [], [], ['VSCO','Lightroom']),
    ('smm target_ads content_plan branding', 'smm_manager', [], [], ['анализ целевой аудитории']),
    ('figma photoshop illustrator indesign after_effects prototyping design_systems yandex_metrica google_analytics html css graphic_design branding layout tilda animation',
     'ui_ux_designer web_designer', [], ['remote'], ['Principle','Wix','Readymag']),
    ('figma prototyping animation after_effects', 'ui_ux_designer', [], ['remote'], []),
    ('smm target_ads content_strategy content_plan retouching', 'smm_manager targetologist', [10000], [], []),
    ('figma canva', 'unknown:marketplace_designer', [100], [], ['инфографика','нейросети без названия']),
    ('', 'ui_ux_designer unknown:product_designer', [], [], []),
    ('figma photoshop', 'ui_ux_designer', [], ['remote'], []),
    ('branding retouching', 'photographer unknown:brand_visualizer', [], [], ['фотосъемка','moodboard']),
    ('', 'unknown:infoproduct_technician', [], [], ['GetCourse','Telegram-боты','DNS','SSL','FBL']),
    ('copywriting literacy editing content_plan', 'copywriter', [350,500,150,250], [], []),
    ('web_analytics google_analytics yandex_metrica english', 'account_manager', [], ['onsite','remote'], ['AdFox','деловая переписка','подготовка презентаций без выступления']),
    ('graphic_design composition color_theory photoshop figma after_effects', 'graphic_designer', [], ['remote'], ['дизайн печатной продукции']),
    ('motion_design', 'motion_designer', [], [], ['озвучка','создание рекламных тезисов']),
    ('figma sketch photoshop illustrator indesign animation prepress', 'designer', [], ['onsite'], []),
    ('', 'unknown:media_community_lead', [30000], [], []),
    ('', 'journalist', [], [], ['интервьюирование','поиск тем']),
    ('', 'marketer', [], [], ['digital-маркетинг','управление рекламными кампаниями']),
    ('ms_office word excel english', 'assistant', [], ['onsite'], ['1С']),
    ('', 'ui_ux_designer', [], [], []),
    ('literacy english', 'smm_manager pr_manager', [50000,70000], ['remote'], ['аналитика социальных сетей','графические редакторы без названия']),
    ('', 'editor', [30000], [], []),
    ('copywriting content_plan', 'copywriter', [2500,5000,7000], [], []),
]
