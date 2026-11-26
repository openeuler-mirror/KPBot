package main

import (
	"context"

	echo "demo/kitex_gen/echo"
)

// EchoImpl implements the last service interface defined in the IDL.
type EchoImpl struct{}

// Echo reflects the request back so benchmarks measure the framework, not business logic.
func (s *EchoImpl) Echo(ctx context.Context, req *echo.EchoRequest) (resp *echo.EchoResponse, err error) {
	return &echo.EchoResponse{
		Msg:     req.Msg,
		Id:      req.Id,
		Nums:    req.Nums,
		Payload: req.Payload,
	}, nil
}

func (s *EchoImpl) Ping(ctx context.Context, req *echo.EchoRequest) (resp *echo.EchoResponse, err error) {
	return s.Echo(ctx, req)
}
