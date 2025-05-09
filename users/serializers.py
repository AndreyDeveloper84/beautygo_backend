from rest_framework import serializers
from .models import User
from .models import Service
from .models import Profile

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ('username', 'password', 'email', 'phone', 'role')

    def create(self, validated_data):
        user = User.objects.create_user(
            username=validated_data['username'],
            email=validated_data.get('email'),
            phone=validated_data.get('phone'),
            role=validated_data.get('role'),
            password=validated_data['password']
        )
        return user

class ServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Service
        fields = '__all__'
        read_only_fields = ['specialist']

class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = ['id', 'full_name', 'avatar', 'bio', 'city', 'experience_years']