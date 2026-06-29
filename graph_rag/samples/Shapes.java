package com.example.shapes;

interface Shape {
    double area();
    String name();
}

abstract class Base implements Shape {
    public String name() {
        return "shape";
    }
}

class Circle extends Base {
    private double r;

    @Override
    public double area() {
        return 3.14 * r * r;
    }

    @Override
    public String name() {
        return "circle";
    }
}
