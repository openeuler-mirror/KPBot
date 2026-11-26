namespace go echo

struct EchoRequest {
    1: string msg
    2: i64 id
    3: list<i64> nums
    4: string payload
}

struct EchoResponse {
    1: string msg
    2: i64 id
    3: list<i64> nums
    4: string payload
}

service Echo {
    EchoResponse Echo(1: EchoRequest req)
    EchoResponse Ping(1: EchoRequest req)
}
