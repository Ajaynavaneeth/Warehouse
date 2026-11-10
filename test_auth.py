import unittest

from auth import LoginThrottle, make_token, verify_token

SECRET = "x" * 40


class TokenTests(unittest.TestCase):
    def test_valid_token_verifies(self):
        self.assertTrue(verify_token(SECRET, make_token(SECRET, 3600)))

    def test_wrong_secret_rejected(self):
        self.assertFalse(verify_token("y" * 40, make_token(SECRET, 3600)))

    def test_expired_token_rejected(self):
        t = make_token(SECRET, 60, now=1_000_000)
        self.assertTrue(verify_token(SECRET, t, now=1_000_030))
        self.assertFalse(verify_token(SECRET, t, now=1_000_061))

    def test_tampered_token_rejected(self):
        exp, sig = make_token(SECRET, 60).split(".")
        self.assertFalse(verify_token(SECRET, f"{int(exp) + 99999}.{sig}"))  # extend expiry
        self.assertFalse(verify_token(SECRET, f"{exp}.{'0' * len(sig)}"))

    def test_garbage_rejected(self):
        for bad in ["", "abc", ".", "1.", ".abc", "12.34.56", "١٢٣.ff", None, 123]:
            self.assertFalse(verify_token(SECRET, bad), repr(bad))


class ThrottleTests(unittest.TestCase):
    def test_blocks_after_max_fails_then_recovers(self):
        t = LoginThrottle(max_fails=3, window=100)
        for i in range(3):
            self.assertFalse(t.blocked("ip", now=i))
            t.fail("ip", now=i)
        self.assertTrue(t.blocked("ip", now=10))
        self.assertFalse(t.blocked("other", now=10))
        self.assertFalse(t.blocked("ip", now=200))  # window passed

    def test_memory_is_released(self):
        t = LoginThrottle(max_fails=2, window=10)
        t.fail("a", now=0)
        t.blocked("a", now=100)
        self.assertEqual(len(t._fails), 0)


if __name__ == "__main__":
    unittest.main()
