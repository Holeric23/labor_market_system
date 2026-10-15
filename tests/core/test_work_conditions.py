import unittest
from market_system.core.extraction.work_conditions import extract_work_conditions


class WorkConditionsTests(unittest.TestCase):
    def extract(self,text,kind='vacancy'):
        result=extract_work_conditions(text,kind)
        for m in result['mentions']:
            self.assertEqual(text[m['start']:m['end']],m['raw'])
        return result

    def test_dimensions_are_independent(self):
        r=self.extract('Полная занятость. Работа удаленно. График 5/2, с 10 до 19. 40 часов в неделю.')
        self.assertEqual(r['work_format']['value'],'remote')
        self.assertEqual(r['employment_type']['value'],'full_time')
        self.assertEqual(r['schedule']['value'],'5/2')
        self.assertEqual(r['working_hours']['value'],'10:00–19:00')
        self.assertEqual(r['hours_per_week']['value'],40)
        self.assertIsNone(self.extract('Полная занятость')['work_format']['value'])

    def test_alternatives_are_not_hybrid(self):
        r=self.extract('Офис или удаленка на ваш выбор')
        self.assertEqual(r['work_format']['values'],['onsite','remote'])
        self.assertEqual(r['work_format']['status'],'multiple')
        self.assertIsNone(r['work_format']['value'])
        r=self.extract('Гибрид: 3 дня в офисе, 2 дня удаленно.')
        self.assertEqual(r['work_format']['value'],'hybrid')

    def test_negation_and_nonexclusive_negation(self):
        r=self.extract('Удаленка не предусмотрена. Работа в офисе.')
        self.assertEqual(r['work_format']['value'],'onsite')
        r=self.extract('Не рассматриваю офис, только удаленно.','resume')
        self.assertEqual(r['work_format']['value'],'remote')
        r=self.extract('Не только офис, но и удаленка.')
        self.assertEqual(r['work_format']['values'],['onsite','remote'])

    def test_history_and_conditions(self):
        r=self.extract('Раньше работал удаленно, сейчас ищу работу в офисе.','resume')
        self.assertEqual(r['work_format']['value'],'onsite')
        r=self.extract('После испытательного срока возможна удаленка.')
        self.assertEqual(r['work_format']['conditional_values'],['remote'])
        self.assertEqual(r['work_format']['status'],'needs_review')
        self.assertIsNone(r['work_format']['value'])

    def test_incidental_mentions_and_urls(self):
        r=self.extract('Управление удаленной командой. Наш офис находится в Москве. Сайт https://a.ru/remote/5/2')
        self.assertEqual(r['work_format']['values'],[])
        self.assertEqual(r['schedule']['values'],[])

    def test_night_shift_and_english(self):
        r=self.extract('Part-time. Remote. Flexible hours. Сменный график 2/2, ночные смены 22:00–06:00.')
        self.assertEqual(r['work_format']['value'],'remote')
        self.assertEqual(r['employment_type']['value'],'part_time')
        self.assertEqual(r['working_hours']['value'],'22:00–06:00')
        self.assertEqual(set(r['schedule']['values']),{'flexible','shift','night','2/2'})

    def test_conflict_and_absence(self):
        r=self.extract('Удаленка возможна. Удаленка не предусмотрена.')
        self.assertTrue(r['work_format']['conflicting'])
        self.assertIsNone(r['work_format']['value'])
        r=self.extract('Встреча 05/02/2026. Зарплата 100000 рублей.')
        self.assertEqual(r['schedule']['status'],'not_found')
        self.assertEqual(r['working_hours']['status'],'not_found')


if __name__=='__main__':unittest.main()
