import requests

url = "http://127.0.0.1:8000"
query = input()
if query == "get":
    resp = requests.post(f"{url}/get/").json()
    for message in resp["Messages"]:
        print(message)
else:
    message = """
    ll binpow(ll b, ll m, ll mod = MAXLL) {
    ll ans = 1 % mod;
    ll x = b;
    while (m != 0) {
        if (m % 2 == 1) ans *= x;
        ans %= mod;
        x *= x;
        x %= mod;
        m /= 2;
    }
    return ans;
    }"""
    message = input()
    resp = requests.post(f"{url}/send/", json={"message": message})
    print(resp.content)
