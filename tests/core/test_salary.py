import unittest

from market_system.core.extraction.salary import extract_salary


class SalaryTests(unittest.TestCase):
    def test_range_and_qualifiers(self):
        text = 'Дизайнер. Зарплата от 100 до 150 тыс. руб. в месяц на руки'
        r = extract_salary(text)
        self.assertEqual(r['status'], 'extracted')
        self.assertEqual((r['salary_from'],r['salary_to'],r['currency'],r['salary_period'],r['tax_mode']),
                         (100000,150000,'RUB','month','net'))
        m=r['mentions'][0]
        self.assertEqual(text[m['start']:m['end']],m['raw'])

    def test_formats(self):
        for text, lower, upper, cur, period in [
            ('ЗП от 100 000 ₽',100000,None,'RUB',None),
            ('Оплата до 80к рублей',None,80000,'RUB',None),
            ('Зарплата $2000–3000 в месяц gross',2000,3000,'USD','month'),
            ('Оплата 1,5–2 млн руб.',1500000,2000000,'RUB',None),
            ('ЗП 80.000–120.000 руб.',80000,120000,'RUB',None),
            ('Оплата 5000 руб за макет',5000,5000,'RUB','project'),
            ('Оплата 1000 руб/час',1000,1000,'RUB','hour'),
            ('**ЗП:** 80\u00a0000 – 160\u00a0000 ₽',80000,160000,'RUB',None),
            ('Зарплата:\n100 000 рублей',100000,100000,'RUB',None),
            ('ЗП 70-80 т.р.',70000,80000,'RUB',None),
            ('Зарплатные ожидания: 75 000₽ - 100 000₽',75000,100000,'RUB',None),
        ]:
            with self.subTest(text=text):
                r=extract_salary(text)
                self.assertEqual(r['status'],'extracted')
                self.assertEqual((r['salary_from'],r['salary_to'],r['currency'],r['salary_period']), (lower,upper,cur,period))

    def test_no_invented_units(self):
        r=extract_salary('Оплата 25 000')
        self.assertEqual(r['salary_from'],25000)
        self.assertIsNone(r['currency'])
        self.assertIsNone(r['salary_period'])
        self.assertIsNone(r['tax_mode'])
        self.assertEqual(extract_salary('ЗП 100–150 net')['status'],'needs_review')
        self.assertEqual(extract_salary('Оплата от 10 до 15 000 рублей')['status'],'needs_review')
        self.assertEqual(extract_salary('За 5 000₽ ваш бизнес станет доходным')['status'],'needs_review')
        self.assertEqual(extract_salary('Вывела аккаунт на доход от 500к в месяц')['status'],'needs_review')

    def test_non_salary_numbers(self):
        for text in ['Опыт 3 года, график 5/2, телефон +79991234567',
                     'Опыт работы с бюджетом от 300 тыс. руб. в месяц',
                     'Оборот 250 000 000 ₽ в год', 'Зарплата: 15% от сделки',
                     'Оплата по договорённости']:
            with self.subTest(text=text):
                self.assertEqual(extract_salary(text)['status'],'not_found')

    def test_ambiguities_not_promoted(self):
        for text in ['ЗП 150–100 тыс руб', 'Оплата 100 тыс руб + бонус',
                     '100000 рублей', 'ЗП 100 тыс руб net gross',
                     'Зарплата 100 тыс руб, после испытательного срока 120 тыс руб']:
            with self.subTest(text=text):
                r=extract_salary(text)
                self.assertEqual(r['status'],'needs_review')
                self.assertIsNone(r['salary_from'])
                self.assertTrue(r['mentions'])


if __name__ == '__main__':
    unittest.main()
