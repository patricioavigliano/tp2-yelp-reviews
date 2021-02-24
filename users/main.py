from consumers import CounterBy
from health_server import HealthServer


def main():
    healthServer = HealthServer()
    while True:
        counter = CounterBy(keyId="user_id", exchange="reviews", routing_key="users")
        user_count = counter.count()
        user_count_5 = dict([u for u in user_count.items() if u[1] >= 5])
        user_count_50 = dict([u for u in user_count_5.items() if u[1] >= 50])
        user_count_150 = dict([u for u in user_count_50.items() if u[1] >= 1500])
        counter.forward("reviews", "stars5", user_count_50)
        counter.forward("reviews", "comment", user_count_5)

        print(len(user_count_50), " Users with more than 50 reviews")
        counter.reply(("user_150", user_count_150))
        # counter.reply(("user_50", user_count_50))
        counter.close()
    healthServer.stop()


if __name__ == "__main__":
    main()
