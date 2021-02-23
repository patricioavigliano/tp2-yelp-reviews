import time
import json
import os
import pika
import zipfile
import pprint

REVIEWS_DATASET_FILEPATH = "data/yelp_academic_dataset_review.json.zip"
BUSINESS_DATASET_FILEPATH = "data/yelp_academic_dataset_business.json.zip"
CHUNK_SIZE = 1 * 1024 * 1024
MAX_REVIEWS = 500000
# 9000000000  #  8021122 Hasta 1 chunk de más
MAX_BUSINESS = 20000  # 209393
QUERIES = 5


def main():
    print("Setup rabbitmq connection")
    amqp_url = os.environ["AMQP_URL"]
    parameters = pika.URLParameters(amqp_url)
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.exchange_declare(exchange="data", exchange_type="direct")

    time.sleep(10)
    print("Start processing")

    def publish__file(file_path, chunk_size, max_size, publish):
        with open(file_path, "r") as f:
            item_count = 0
            lines = f.readlines(chunk_size)
            while lines and item_count < max_size:
                chunk = [json.loads(line) for line in lines]
                item_count += len(chunk)
                publish(body={"data": chunk})
                lines = f.readlines(chunk_size)
        print(item_count, "items read from ", file_path)

    def publish_file(file_path, chunk_size, max_size, publish):
        item_count = 0
        with zipfile.ZipFile(file_path) as z:
            for zname in z.namelist():
                with z.open(zname) as f:
                    lines = f.readlines(chunk_size)
                    while lines and item_count < max_size:
                        chunk = [json.loads(line) for line in lines]
                        item_count += len(chunk)
                        publish(body={"data": chunk})
                        lines = f.readlines(chunk_size)
        print(item_count, "items read from ", file_path)

    try:
        publish_file(
            file_path=BUSINESS_DATASET_FILEPATH,
            chunk_size=CHUNK_SIZE,
            max_size=MAX_BUSINESS,
            publish=lambda body: channel.basic_publish(
                exchange="data",
                routing_key="business",
                body=json.dumps(body),
            ),
        )
    finally:
        channel.basic_publish(
            exchange="data",
            routing_key="business",
            body=json.dumps({"data": None}),
        )

    print("Callback queue setup")
    callback_queue = channel.queue_declare(queue="", exclusive=True).method.queue

    try:
        publish_file(
            file_path=REVIEWS_DATASET_FILEPATH,
            chunk_size=CHUNK_SIZE,
            max_size=MAX_REVIEWS,
            publish=lambda body: channel.basic_publish(
                exchange="data",
                routing_key="review",
                body=json.dumps(body),
            ),
        )
    finally:
        channel.basic_publish(
            exchange="data",
            routing_key="review",
            properties=pika.BasicProperties(
                reply_to=callback_queue,
            ),
            body=json.dumps({"data": None}),
        )

    print("Waiting for Report")
    connection.process_data_events()
    report = {}
    for method, props, body in channel.consume(callback_queue, auto_ack=True):
        key, val = json.loads(body.decode("utf-8"))
        report[key] = val
        if len(report) >= 5:
            break
    print("report: ", flush=False)
    pprint.pprint(report)

    channel.cancel()
    connection.close()


if __name__ == "__main__":
    main()
