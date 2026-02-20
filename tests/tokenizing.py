# Unit tests for testing we can tokenize text at a certain speed

import unittest
import time

import tokenizer

def timing_outline(test, elapsed_time):
    print(f"{test} execution time: {elapsed_time:.4f} seconds")
    
class TokenizingPerformance(unittest.TestCase):

    def test_1st_page_tokenizing(self):
        # 1st largest page tokenizing

        with open("tests/shakespeare.txt", "r") as file:
            text = file.read()

        start_time = time.perf_counter()

        # Tokenize the page and time performance
        result = tokenizer.tokenize_all(text)

        end_time = time.perf_counter()
        elapsed_time = end_time - start_time

        # Print the total time for each test
        timing_outline("Tokenizing 5.2MB page", elapsed_time)

        # Assert the time it takes to tokenize stuff falls under a certain threshold (not super useful but could be if you're trying to optimize to a certan point)
        self.assertLess(elapsed_time, 3.0)

        # Assert that the tokens are the right tokens. Need to store the tokens locally but I think it's not super useful right now so I'm not going to finish it
        #self.assertEqual(result, None)

    def test_2nd_page_tokenizing(self):
        # 2nd largest page tokenizing

        with open("tests/moby.txt", "r") as file:
            text = file.read()
        start_time = time.perf_counter()
        result = tokenizer.tokenize_all(text)
        end_time = time.perf_counter()
        elapsed_time = end_time - start_time
        timing_outline("Tokenizing 1.2MB page", elapsed_time)
        self.assertLess(elapsed_time, 1.0)
        #self.assertEqual(result, None)

    def test_3rd_page_tokenizing(self):
        # 3rd largest page tokenizing

        with open("tests/odyssey.txt", "r") as file:
            text = file.read()
        start_time = time.perf_counter()
        result = tokenizer.tokenize_all(text)
        end_time = time.perf_counter()
        elapsed_time = end_time - start_time
        timing_outline("Tokenizing 690KB page", elapsed_time)
        self.assertLess(elapsed_time, 1.0)
        #self.assertEqual(result, None)

    def test_4th_page_tokenizing(self):
        # 4th largest page tokenizing

        with open("tests/gatsby.txt", "r") as file:
            text = file.read()
        start_time = time.perf_counter()
        result = tokenizer.tokenize_all(text)
        end_time = time.perf_counter()
        elapsed_time = end_time - start_time
        timing_outline("Tokenizing 300KB page", elapsed_time)
        self.assertLess(elapsed_time, 1.0)
        #self.assertEqual(result, None)

    def test_5th_page_tokenizing(self):
        # 5th largest page tokenizing

        with open("tests/alice.txt", "r") as file:
            text = file.read()
        start_time = time.perf_counter()
        result = tokenizer.tokenize_all(text)
        end_time = time.perf_counter()
        elapsed_time = end_time - start_time
        timing_outline("Tokenizing 167KB page", elapsed_time)
        self.assertLess(elapsed_time, 1.0)
        #self.assertEqual(result, None)



suite = unittest.TestLoader().loadTestsFromTestCase(TokenizingPerformance)
runner = unittest.TextTestRunner(verbosity=0)
result = runner.run(suite)
print(f'Tests run: {result.testsRun}') 