import unittest
from market_system.core.extraction.occupations import OccupationExtractor


class OccupationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.extractor=OccupationExtractor()

    def test_target_not_colleague(self):
        text='Ищем дизайнера для работы с редактором'
        result=self.extractor.extract(text,'vacancy')
        self.assertEqual(result['occupation_ids'],['designer'])
        self.assertEqual(next(m['role'] for m in result['mentions'] if m['occupation_id']=='editor'),'other_person')
        for m in result['mentions']:self.assertEqual(text[m['start']:m['end']],m['raw'])

    def test_specific_title_and_multiple_positions(self):
        self.assertEqual(self.extractor.extract('Требуется графический дизайнер','vacancy')['occupation_ids'],['graphic_designer'])
        r=self.extractor.extract('Ищем дизайнера и копирайтера','vacancy')
        self.assertEqual(set(r['occupation_ids']),{'designer','copywriter'})
        self.assertEqual(r['status'],'multiple')
        self.assertIsNone(r['primary_occupation_id'])

    def test_resume_current_vs_previous(self):
        r=self.extractor.extract('Раньше работал копирайтером, сейчас ищу работу редактором','resume')
        self.assertEqual(r['occupation_ids'],['editor'])
        self.assertEqual(next(m['role'] for m in r['mentions'] if m['occupation_id']=='copywriter'),'historical')
        self.assertEqual(self.extractor.extract('Я графический дизайнер','resume')['occupation_ids'],['graphic_designer'])

    def test_tools_and_urls_are_not_target_jobs(self):
        for text in ['Владею Adobe Illustrator','Навыки: Photoshop и Figma','Контакты: https://site.com/designer']:
            self.assertEqual(self.extractor.extract(text,'resume')['occupation_ids'],[])
        self.assertEqual(self.extractor.extract('Ищу копирайтера для своего проекта','resume')['occupation_ids'],[])

    def test_negative_and_empty(self):
        self.assertEqual(self.extractor.extract('Дизайнер не требуется','vacancy')['occupation_ids'],[])
        self.assertEqual(self.extractor.extract('','vacancy')['status'],'not_found')

    def test_no_recruiting_duties_as_target(self):
        text='Ищем контент-продюсера\nЧто делать:\nНаходить исполнителей (дизайнеры, верстальщики).'
        self.assertEqual(self.extractor.extract(text,'vacancy')['occupation_ids'],['content_producer'])
        self.assertEqual(self.extractor.extract('Менеджеров по продажам мы НЕ ищем','vacancy')['occupation_ids'],[])
        self.assertEqual(self.extractor.extract('UI / UX / Продуктовый дизайнер с четырехлетним опытом работы','resume')['occupation_ids'],['ui_ux_designer'])

    def test_title_at_end_of_long_paragraph(self):
        text='Мы помогаем брендам развивать продукты. Сейчас ищем в нашу дружную команду для работы над новым большим международным проектом с интересными клиентами и сложными визуальными задачами графического дизайнера.'
        self.assertEqual(self.extractor.extract(text,'vacancy')['occupation_ids'],['graphic_designer'])
        text='Мы расскажем о компании. '+('Подробное описание продукта. '*10)+'В команду требуется редактор.'
        self.assertEqual(self.extractor.extract(text,'vacancy')['occupation_ids'],['editor'])

    def test_reversed_cue_and_explicit_position(self):
        self.assertEqual(self.extractor.extract('Для нового проекта графический дизайнер нужен в команду.','vacancy')['occupation_ids'],['graphic_designer'])
        self.assertEqual(self.extractor.extract('Наше агентство растет. Открыта позиция: копирайтер.','vacancy')['occupation_ids'],['copywriter'])

    def test_negation_does_not_hide_another_target(self):
        self.assertEqual(self.extractor.extract('Дизайнер не нужен, ищем редактора.','vacancy')['occupation_ids'],['editor'])
        self.assertEqual(self.extractor.extract('Ищем дизайнера, редактор не требуется.','vacancy')['occupation_ids'],['designer'])

    def test_title_after_heading_and_team_experience(self):
        self.assertEqual(self.extractor.extract('Мы ищем:\n— графического дизайнера\nТребования:\nОпыт работы с редактором.','vacancy')['occupation_ids'],['graphic_designer'])
        self.assertEqual(self.extractor.extract('В команде 10 дизайнеров. Ищем редактора.','vacancy')['occupation_ids'],['editor'])
        self.assertEqual(self.extractor.extract('Ищем редактора с опытом работы дизайнером.','vacancy')['occupation_ids'],['editor'])

    def test_resume_profession_in_body(self):
        self.assertEqual(self.extractor.extract('Привет! Меня зовут Анна. По профессии я графический дизайнер. Сейчас рассматриваю работу редактором.','resume')['occupation_ids'],['editor','graphic_designer'])


if __name__=='__main__':unittest.main()
