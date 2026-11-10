package platform

import (
	"go/ast"
	"go/parser"
	"go/token"
	"strings"
)

func evalCondition(source string, vars M) bool {
	source = strings.ReplaceAll(strings.ReplaceAll(strings.ReplaceAll(source, " and ", " && "), " or ", " || "), "not ", "!")
	expr, e := parser.ParseExpr(source)
	if e != nil {
		fail(422, "条件表达式无效")
	}
	var eval func(ast.Expr) any
	eval = func(e ast.Expr) any {
		switch x := e.(type) {
		case *ast.ParenExpr:
			return eval(x.X)
		case *ast.BasicLit:
			if x.Kind == token.INT || x.Kind == token.FLOAT {
				return num(x.Value)
			}
			fail(422, "条件只支持数值与布尔值")
		case *ast.Ident:
			if x.Name == "true" {
				return true
			}
			if x.Name == "false" {
				return false
			}
			if v, ok := vars[x.Name]; ok {
				return v
			}
			fail(422, "未知条件变量："+x.Name)
		case *ast.UnaryExpr:
			v := eval(x.X)
			if x.Op == token.NOT {
				return !boolOf(v)
			}
			if x.Op == token.SUB {
				return -num(v)
			}
		case *ast.BinaryExpr:
			l, r := eval(x.X), eval(x.Y)
			switch x.Op {
			case token.LAND:
				return boolOf(l) && boolOf(r)
			case token.LOR:
				return boolOf(l) || boolOf(r)
			case token.EQL:
				return num(l) == num(r)
			case token.NEQ:
				return num(l) != num(r)
			case token.GTR:
				return num(l) > num(r)
			case token.GEQ:
				return num(l) >= num(r)
			case token.LSS:
				return num(l) < num(r)
			case token.LEQ:
				return num(l) <= num(r)
			case token.ADD:
				return num(l) + num(r)
			case token.SUB:
				return num(l) - num(r)
			case token.MUL:
				return num(l) * num(r)
			}
		}
		fail(422, "不支持的条件表达式")
		return false
	}
	return boolOf(eval(expr))
}
